from setuptools import setup, find_packages

setup(
    name='rag',
    version='0.1.0',
    packages=find_packages(),
    include_package_data=True,
    package_data={
        'rag': ['wwwroot/**/*', 'models/*.json', 'conf/*.json'],
    },
    install_requires=[],
)
