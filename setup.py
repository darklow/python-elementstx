#!/usr/bin/env python

from setuptools import setup, find_packages
import os

here = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(here, 'README.md')) as f:
    README = f.read()

requires = ['python-bitcointx>=1.0.0,<2']

version_ns = {}
ver_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'elementstx', 'version.py')
with open(ver_path) as ver_file:
    exec(ver_file.read(), version_ns)

setup(name='python-elementstx',
      version=version_ns['__version__'],
      description='Elements module for python-bitcointx',
      long_description=README,
      long_description_content_type='text/markdown',
      classifiers=[
          "Programming Language :: Python :: 3.6",
          "Programming Language :: Python :: 3.7",
          "Programming Language :: Python :: 3.8",
          "License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)",
      ],
      python_requires='>=3.6',
      url='https://github.com/Simplexum/python-elementstx',
      keywords='bitcoin,elements',
      packages=find_packages(),
      zip_safe=False,
      install_requires=requires,
      test_suite="elementstx.tests"
     )
