from setuptools import setup, find_packages

setup(
    name='shared-python-utils',
    version='1.0',
    packages=find_packages(),  # This will automatically find the "shared-python-utils" package
    include_package_data=True,  # To include non-code files (if needed)
    install_requires=[],        # Add dependencies if there are any
)