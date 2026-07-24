from setuptools import setup, find_packages

setup(
    name='rag',
    version='0.1.0',
    packages=find_packages(),
    include_package_data=True,
    data_files=[
        ('rag/wwwroot', []),  # placeholder for discovery
    ],
    package_data={
        '': ['wwwroot/**/*', 'json/*.json', 'models/*.json', 'conf/*.json'],
    },
    install_requires=[],
)
