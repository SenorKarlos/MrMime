#!/usr/bin/env python

import os
from setuptools import setup, find_packages
from pip._internal.req import parse_requirements

setup_dir = os.path.dirname(os.path.realpath(__file__))
path_req = os.path.join(setup_dir, 'requirements.txt')
install_reqs = parse_requirements(path_req, session=False)

reqs = [str(ir.req) for ir in install_reqs]

setup(name='MrMime',
      author = 'sLoPPydrive',
      description = 'Pokemon GO client library mimicing the original app',
      version = '0.9.0',
      url = 'https://github.com/SenorKarlos/MrMime',
      download_url = "https://github.com/SenorKarlos/MrMime/releases",
      packages = find_packages(),
      install_requires = reqs
      )
