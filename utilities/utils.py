from conf import settings as S

import errno
import functools
import os
import random
import re
import uuid
import collections
from pykwalify import core as pykwalify_core
from pykwalify import errors as pykwalify_errors
import yaml


def read_file(file_name, base_dir='', alias_mapper=None):
    """
    Read Files
    """
    full_path = os.path.normpath(os.path.join(base_dir, file_name))

    if alias_mapper:  # interpret file_name as alias
        alias_path = resolve_relative_path(alias_mapper(file_name))
        if alias_path:
            full_path = alias_path
            LOG.info('Alias "%s" is resolved into file "%s"',
                     file_name, full_path)

    if not os.path.exists(full_path):
        full_path = os.path.normpath(os.path.join(
            os.path.dirname(__import__('testvnf').__file__), '../', file_name))
        if not os.path.exists(full_path):
            msg = ('File %s not found by absolute nor by relative path' %
                   file_name)
            LOG.error(msg)
            raise IOError(msg)

    fd = None
    try:
        fd = open(full_path)
        return fd.read()
    except IOError as e:
        LOG.error('Error reading file: %s', e)
        raise
    finally:
        if fd:
            fd.close()


def write_file(data, file_name, base_dir=''):
    """
    Write to file
    """
    full_path = os.path.normpath(os.path.join(base_dir, file_name))
    fd = None
    try:
        fd = open(full_path, 'w')
        return fd.write(data)
    except IOError as e:
        LOG.error('Error writing file: %s', e)
        raise
    finally:
        if fd:
            fd.close()


def read_yaml_file(file_name):
    """
    Read Yaml File
    """
    raw = read_file(file_name)
    return read_yaml(raw)


def read_yaml(raw):
    """
    Read YAML
    """
    try:
        parsed = yaml.safe_load(raw)
        return parsed
    except Exception as e:
        LOG.error('Failed to parse input %(yaml)s in YAML format: %(err)s',
                  dict(yaml=raw, err=e))
        raise


def split_address(address):
    """
    Split addresses
    """
    try:
        host, port = address.split(':')
    except ValueError:
        raise ValueError('Invalid address: %s, "host:port" expected', address)
    return host, port


def random_string(length=6):
    """
    Generate Random String
    """
    return ''.join(random.sample('adefikmoprstuz', length))


def make_record_id():
    """
    Create record-ID
    """
    return str(uuid.uuid4())

def strict(strc):
    """
    Strict Check
    """
    return re.sub(r'[^\w\d]+', '_', re.sub(r'\(.+\)', '', strc)).lower()


def validate_yaml(data, schema):
    """
    Validate Yaml
    """
    c = pykwalify_core.Core(source_data=data, schema_data=schema)
    try:
        c.validate(raise_exception=True)
    except pykwalify_errors.SchemaError as e:
        raise Exception('File does not conform to schema: %s' % e)


def pack_openstack_params():
    """
    Packe Openstack Parameters
    """
    if not S.hasValue('OS_AUTH_URL'):
        raise Exception(
            'OpenStack authentication endpoint is missing')

    params = dict(auth=dict(username=S.getValue('OS_USERNAME'),
                            password=S.getValue('OS_PASSWORD'),
                            auth_url=S.getValue('OS_AUTH_URL')),
                  os_region_name=S.getValue('OS_REGION_NAME'),
                  os_cacert=S.getValue('OS_CA_CERT'),
                  os_insecure=S.getValue('OS_INSECURE'))

    if S.hasValue('OS_PROJECT_NAME'):
        value = S.getValue('OS_PROJECT_NAME')
        params['auth']['project_name'] = value
    if S.hasValue('OS_PROJECT_DOMAIN_NAME'):
        value = S.getValue('OS_PROJECT_DOMAIN_NAME')
        params['auth']['project_domain_name'] = value
    if S.hasValue('OS_USER_DOMAIN_NAME'):
        value = S.getValue('OS_USER_DOMAIN_NAME')
        params['auth']['user_domain_name'] = value
    if S.hasValue('OS_INTERFACE'):
        value = S.getValue('OS_INTERFACE')
        params['os_interface'] = value
    if S.hasValue('OS_API_VERSION'):
        value = S.getValue('OS_API_VERSION')
        params['identity_api_version'] = value
    if S.hasValue('OS_PROFILE'):
        value = S.getValue('OS_PROFILE')
        params['os_profile'] = value
    return params
