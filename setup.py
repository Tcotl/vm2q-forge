from setuptools import find_packages, setup


setup(
    name="vm2q-forge",
    version="0.1.0",
    description="VMware to qcow2 conversion and VirtIO preparation toolkit.",
    packages=find_packages(include=["vmware2qcow2", "vmware2qcow2.*"]),
    package_data={"vmware2qcow2": ["drivers/*.iso", "drivers/README.md"]},
    entry_points={"console_scripts": ["vmware2qcow2=vmware2qcow2.cli:main"]},
    python_requires=">=3.10",
)
