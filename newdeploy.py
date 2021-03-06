import collections
import functools
import random
import sys
import os
import copy
import json
import jinja2
import shutil
import datetime
import time
import logging

from conf import merge_spec
from conf import settings as S

from utilities import utils
from osclients import heat
from osclients import neutron
from osclients import nova
from osclients import openstack

LOG = logging.getLogger(__name__)
_CURR_DIR = os.path.dirname(os.path.realpath(__file__))

class DeploymentException(Exception):
    """ Exception Handling """
    pass


def prepare_for_cross_az(compute_nodes, zones):
    """
    Deployment across Availability Zones
    """
    if len(zones) != 2:
        LOG.warn('cross_az is specified, but len(zones) is not 2')
        return compute_nodes

    masters = []
    slaves = []
    for node in compute_nodes:
        if node['zone'] == zones[0]:
            masters.append(node)
        else:
            slaves.append(node)

    res = []
    for i in range(min(len(masters), len(slaves))):
        res.append(masters[i])
        res.append(slaves[i])

    return res


def generate_agents(compute_nodes, accommodation, unique):
    """
    Generate TestVNF Instances
    """
    print('Number of compute nodes')
    print(compute_nodes)
    density = accommodation.get('density') or 1

    zones = accommodation.get('zones')
    if zones:
        compute_nodes = [
            c for c in compute_nodes if c['zone'] in zones or
            ':'.join(filter(None, [c['zone'], c['host']])) in zones]
        if 'cross_az' in accommodation:
            compute_nodes = prepare_for_cross_az(compute_nodes, zones)

    best_effort = accommodation.get('best_effort', False)
    compute_nodes_requested = accommodation.get('compute_nodes')
    if compute_nodes_requested:
        if compute_nodes_requested > len(compute_nodes):
            print(str(len(compute_nodes)))
            if best_effort:
                LOG.warn('Allowing best_effort accommodation: '
                         'compute nodes requested: %(req)d: '
                         'available: %(avail)d available' %
                         dict(req=compute_nodes_requested,
                              avail=len(compute_nodes)))
            else:
                raise DeploymentException(
                    'Exception Not enough compute nodes %(cn)s for requested '
                    'instance accommodation %(acc)s' %
                    dict(cn=compute_nodes, acc=accommodation))
        else:
            compute_nodes = random.sample(compute_nodes,
                                          compute_nodes_requested)

    cn_count = len(compute_nodes)
    iterations = cn_count * density

    if 'single_room' in accommodation and 'pair' in accommodation:
        # special case to allow pair, single_room on single compute node
        if best_effort and iterations == 1:
            LOG.warn('Allowing best_effort accommodation: '
                     'single_room, pair on one compute node')
        else:
            iterations //= 2
    node_formula = lambda x: compute_nodes[x % cn_count]

    agents = {}

    for i in range(iterations):
        if 'pair' in accommodation:
            master_id = '%s_master_%s' % (unique, i)
            slave_id = '%s_slave_%s' % (unique, i)
            master = dict(id=master_id, mode='master', slave_id=slave_id)
            slave = dict(id=slave_id, mode='slave', master_id=master_id)

            if 'single_room' in accommodation:
                master_formula = lambda x: i * 2
                slave_formula = lambda x: i * 2 + 1
            elif 'double_room' in accommodation:
                master_formula = lambda x: i
                slave_formula = lambda x: i
            else:  # mixed_room
                master_formula = lambda x: i
                slave_formula = lambda x: i + 1

            m = node_formula(master_formula(i))
            master['node'], master['zone'] = m['host'], m['zone']
            s = node_formula(slave_formula(i))
            slave['node'], slave['zone'] = s['host'], s['zone']

            agents[master['id']] = master
            agents[slave['id']] = slave
        else:
            if 'single_room' in accommodation:
                agent_id = '%s_agent_%s' % (unique, i)
                agents[agent_id] = dict(id=agent_id,
                                        node=node_formula(i)['host'],
                                        zone=node_formula(i)['zone'],
                                        mode='alone')

    if not agents:
        raise DeploymentException('Not enough compute nodes %(cn)s for '
                                  'requested instance accommodation %(acc)s' %
                                  dict(cn=compute_nodes, acc=accommodation))

    # inject availability zone
    for agent in agents.values():
        az = agent['zone']
        if agent['node']:
            az += ':' + agent['node']
        agent['availability_zone'] = az

    return agents


def _get_stack_values(stack_outputs, vm_name, params):
    """
    Collect the output from Heat Stack Deployment
    """
    result = {}
    for param in params:
        o = stack_outputs.get(vm_name + '_' + param)
        if o:
            result[param] = o
    return result


def filter_agents(agents, stack_outputs, override=None):
    """
    Filter Deployed Instances - If Required.
    """
    deployed_agents = {}

    # first pass, ignore non-deployed
    for agent in agents.values():
        stack_values = _get_stack_values(stack_outputs, agent['id'], ['ip'])
        new_stack_values = _get_stack_values(stack_outputs, agent['id'], ['pip'])
        mac_values = _get_stack_values(stack_outputs, agent['id'], ['dmac'])

        if override:
            stack_values.update(override(agent))

        if not stack_values.get('ip'):
            LOG.info('Ignore non-deployed agent: %s', agent)
            continue

        if not new_stack_values.get('pip'):
            LOG.info('Ignore non-deployed agent: %s', agent)
            continue

        if not mac_values.get('dmac'):
            LOG.info('Ignore non-deployed agent: %s', agent)
            continue

        agent.update(stack_values)
        agent.update(new_stack_values)

        # workaround of Nova bug 1422686
        if agent.get('mode') == 'slave' and not agent.get('ip'):
            LOG.info('IP address is missing in agent: %s', agent)
            continue

        deployed_agents[agent['id']] = agent

    # second pass, check pairs
    result = {}
    for agent in deployed_agents.values():
        print(agent.get('mode'))
        print(agent.get('ip'))
        print(agent.get('pip'))
        print(agent.get('dmac'))
        if (agent.get('mode') == 'alone' or
                (agent.get('mode') == 'master' and
                 agent.get('slave_id') in deployed_agents) or
                (agent.get('mode') == 'slave' and
                 agent.get('master_id') in deployed_agents)):
            result[agent['id']] = agent

    return result


def distribute_agents(agents, get_host_fn):
    """
    Distribute TestVNF Instances
    """
    result = {}

    hosts = set()
    buckets = collections.defaultdict(list)
    for agent in agents.values():
        agent_id = agent['id']
        # we assume that server name equals to agent_id
        host_id = get_host_fn(agent_id)

        if host_id not in hosts:
            hosts.add(host_id)
            agent['node'] = host_id
            buckets[agent['mode']].append(agent)
        else:
            LOG.info('Filter out agent %s, host %s is already occupied',
                     agent_id, host_id)

    if buckets['alone']:
        result = dict((a['id'], a) for a in buckets['alone'])
    else:
        for master, slave in zip(buckets['master'], buckets['slave']):
            master['slave_id'] = slave['id']
            slave['master_id'] = master['id']

            result[master['id']] = master
            result[slave['id']] = slave

    return result


def normalize_accommodation(accommodation):
    """
    Planning the Accomodation of TestVNFs
    """
    result = {}

    for s in accommodation:
        if isinstance(s, dict):
            result.update(s)
        else:
            result[s] = True

    # override scenario's availability zone accommodation
    if S.hasValue('SCENARIO_AVAILABILITY_ZONE'):
        result['zones'] =  S.getValue('SCENARIO_AVAILABILITY_ZONE')
    # override scenario's compute_nodes accommodation
    if S.hasValue('SCENARIO_COMPUTE_NODES'):
        result['compute_nodes'] = S.getValue('SCENARIO_COMPUTE_NODES')

    return result


class Deployment(object):
    """
    Main Deployment Class
    """
    def __init__(self):
        """
        Initialize
        """
        self.openstack_client = None
        self.stack_id = None
        self.privileged_mode = True

        # The current run "owns" the support stacks, it is tracked
        # so it can be deleted later.
        self.support_stacks = []
        self.TrackStack = collections.namedtuple('TrackStack', 'name id')

    def connect_to_openstack(self, openstack_params, flavor_name, image_name,
                             external_net, dns_nameservers):
        """
        Connect to Openstack
        """
        LOG.debug('Connecting to OpenStack')

        self.openstack_client = openstack.OpenStackClient(openstack_params)

        self.flavor_name = flavor_name
        self.image_name = image_name

        if S.hasValue('STACK_NAME'):
            self.stack_name = S.getValue('STACK_NAME')
        else:
            self.stack_name = 'testvnf_%s' % utils.random_string()

        self.dns_nameservers = dns_nameservers
        # intiailizing self.external_net last so that other attributes don't
        # remain uninitialized in case user forgets to create external network
        self.external_net = (external_net or
                             neutron.choose_external_net(
                                 self.openstack_client.neutron))

    def _get_compute_nodes(self, accommodation):
        """
        Get available comput nodes
        """
        try:
            comps = nova.get_available_compute_nodes(self.openstack_client.nova,
                                                     self.flavor_name)
            print(comps)
            return comps
        except nova.ForbiddenException:
            # user has no permissions to list compute nodes
            LOG.info('OpenStack user does not have permission to list compute '
                     'nodes - treat him as non-admin')
            self.privileged_mode = False
            count = accommodation.get('compute_nodes')
            if not count:
                raise DeploymentException(
                    'When run with non-admin user the scenario must specify '
                    'number of compute nodes to use')

            zones = accommodation.get('zones') or ['nova']
            return [dict(host=None, zone=zones[n % len(zones)])
                    for n in range(count)]

    #def _deploy_from_hot(self, specification, server_endpoint, base_dir=None):
    def _deploy_from_hot(self, specification,  base_dir=None):
        """
        Perform Heat stack deployment
        """
        accommodation = normalize_accommodation(
            specification.get('accommodation') or
            specification.get('vm_accommodation'))

        agents = generate_agents(self._get_compute_nodes(accommodation),
                                 accommodation, self.stack_name)

        # render template by jinja
        vars_values = {
            'agents': agents,
            'unique': self.stack_name,
        }
        heat_template = utils.read_file(specification['template'],
                                        base_dir=base_dir)
        compiled_template = jinja2.Template(heat_template)
        rendered_template = compiled_template.render(vars_values)
        LOG.info('Rendered template: %s', rendered_template)

        # create stack by Heat
        try:
            merged_parameters = {
#                'server_endpoint': server_endpoint,
                'external_net': self.external_net,
                'image': self.image_name,
                'flavor': self.flavor_name,
                'dns_nameservers': self.dns_nameservers,
            }
        except AttributeError as e:
            LOG.error('Failed to gather required parameters to create '
                      'heat stack: %s', e)
            exit(1)

        merged_parameters.update(specification.get('template_parameters', {}))
        try:
            self.stack_id = heat.create_stack(
                self.openstack_client.heat, self.stack_name,
                rendered_template, merged_parameters, None)
        except heat.exc.StackFailure as err:
            self.stack_id = err.args[0]
            raise

        # get info about deployed objects
        outputs = heat.get_stack_outputs(self.openstack_client.heat,
                                         self.stack_id)
        override = self._get_override(specification.get('override'))

        agents = filter_agents(agents, outputs, override)

        if (not self.privileged_mode) and accommodation.get('density', 1) == 1:
            get_host_fn = functools.partial(nova.get_server_host_id,
                                            self.openstack_client.nova)
            agents = distribute_agents(agents, get_host_fn)

        return agents

    def _get_override(self, override_spec):
        """
        Collect the overrides
        """
        def override_ip(agent, ip_type):
            return dict(ip=nova.get_server_ip(
                self.openstack_client.nova, agent['id'], ip_type))

        if override_spec:
            if override_spec.get('ip'):
                return functools.partial(override_ip,
                                         ip_type=override_spec.get('ip'))


    #def deploy(self, deployment, base_dir=None, server_endpoint=None):
    def deploy(self, deployment, base_dir=None):
        """
        Perform Deployment
        """
        agents = {}

        if not deployment:
            # local mode, create fake agent
            agents.update(dict(local=dict(id='local', mode='alone',
                                          node='localhost')))

        if deployment.get('template'):
            if not self.openstack_client:
                raise DeploymentException(
                    'OpenStack client is not initialized. '
                    'Template-based deployment is ignored.')
            else:
                # deploy topology specified by HOT
                agents.update(self._deploy_from_hot(
                    #deployment, server_endpoint, base_dir=base_dir))
                    deployment, base_dir=base_dir))

        if not agents:
            print("No VM Deployed - Deploy")
            raise Exception('No agents deployed.')

        if deployment.get('agents'):
            # agents are specified statically
            agents.update(dict((a['id'], a) for a in deployment.get('agents')))

        return agents

def read_scenario(scenario_name):
    """
    Collect all Information about the scenario
    """
    scenario_file_name = scenario_name
    LOG.debug('Scenario %s is resolved to %s', scenario_name,
              scenario_file_name)

    scenario = utils.read_yaml_file(scenario_file_name)

    schema = utils.read_yaml_file(S.getValue('SCHEMA'))
    utils.validate_yaml(scenario, schema)

    scenario['title'] = scenario.get('title') or scenario_file_name
    scenario['file_name'] = scenario_file_name

    return scenario

def _extend_agents(agents_map):
    """
    Add More info to deployed Instances
    """
    extended_agents = {}
    for agent in agents_map.values():
        extended = copy.deepcopy(agent)
        if agent.get('slave_id'):
            extended['slave'] = copy.deepcopy(agents_map[agent['slave_id']])
        if agent.get('master_id'):
            extended['master'] = copy.deepcopy(agents_map[agent['master_id']])
        extended_agents[agent['id']] = extended
    return extended_agents

def play_scenario(scenario):
    """
    Deploy a scenario
    """
    deployment = None
    output = dict(scenarios={}, agents={})
    output['scenarios'][scenario['title']] = scenario

    try:
        deployment = Deployment()

        openstack_params = utils.pack_openstack_params()
        try:
            deployment.connect_to_openstack(
                openstack_params, S.getValue('FLAVOR_NAME'),
                S.getValue('IMAGE_NAME'), S.getValue('EXTERNAL_NET'),
                S.getValue('DNS_NAMESERVERS'))
        except Exception as e:
            LOG.warning('Failed to connect to OpenStack: %s. Please '
                        'verify parameters: %s', e, openstack_params)

        base_dir = os.path.dirname(scenario['file_name'])
        scenario_deployment = scenario.get('deployment', {})
        agents = deployment.deploy(scenario_deployment, base_dir=base_dir)

        if not agents:
            print("No VM Deployed - Play-Scenario")
            raise Exception('No agents deployed.')

        agents = _extend_agents(agents)
        output['agents'] = agents
        LOG.debug('Deployed agents: %s', agents)
        print(agents)

        if not agents:
            raise Exception('No agents deployed.')

    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            LOG.info('Caught SIGINT. Terminating')
            record = dict(id=utils.make_record_id(), status='interrupted')
        else:
            error_msg = 'Error while executing scenario: %s' % e
            LOG.exception(e)
    return output

def act():
    """
    Kickstart the Scenario Deployment
    """
    for scenario_name in S.getValue('SCENARIOS'):
        LOG.info('Play scenario: %s', scenario_name)
        print('Play scenario: {}'.format(scenario_name))
        scenario = read_scenario(scenario_name)
        play_output = play_scenario(scenario)
        print(play_output)
        return play_output

def create_vsperf_conffile(agents, tgen):
    """
    Create Configuration file for VSPERF.
    """
    date = datetime.datetime.fromtimestamp(time.time())
    timestamp = date.strftime('%Y-%m-%d_%H-%M-%S')
    if not len(agents):
        print("No agents provided")
        return
    filename = 'vsperf-'+ tgen + '.conf'
    filepath = os.path.join('./testconfs', filename)
    destfilename = 'vsperf-' + tgen + "-" + timestamp + ".conf"
    destfilepath = os.path.join('./testconfs', destfilename)
    shutil.copyfile(filepath, destfilepath)
    print('Using File: ', destfilepath)
    east_chassis_ip = agents[0]['public_ip']
    east_data_ip = agents[0]['private_ip']
    if len(agents) == 2:
        west_chassis_ip = agents[1]['public_ip']
        west_data_ip = agents[1]['private_ip']
    else:
        west_chassis_ip = east_chassis_ip
        west_data_ip = east_chassis_ip
    with open(destfilepath, "+a") as filep:
        if tgen == "spirent":
            filep.write('TRAFFICGEN_STC_EAST_CHASSIS_ADDR = "{0}" \n'.format(east_chassis_ip))
            filep.write('TRAFFICGEN_STC_WEST_CHASSIS_ADDR = "{0}" \n'.format(west_chassis_ip))
        if tgen == "ixnet":
            filep.writelines("TRAFFICGEN_EAST_IXIA_HOST = " +
                        east_chassis_ip + '\n')
            filep.writelines("TRAFFICGEN_WEST_IXIA_HOST = " +
                        west_chassis_ip + '\n')

def main():
    """Main function.
    """
    #args = parse_arguments()

    # define the timestamp to be used by logs and results
    date = datetime.datetime.fromtimestamp(time.time())
    timestamp = date.strftime('%Y-%m-%d_%H-%M-%S')
    S.setValue('LOG_TIMESTAMP', timestamp)


    # configure settings
    S.load_from_dir(os.path.join(_CURR_DIR, 'conf'))
    openstack_params = utils.pack_openstack_params()
    print(openstack_params)
    output = act()
    print(output)

if __name__ == "__main__":
    main()
