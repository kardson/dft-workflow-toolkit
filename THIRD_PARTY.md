# Dependencies and licensing

The candidate contains locally maintained Python modules and documentation. It does not include vendored upstream repositories, commercial executables, potential datasets, or downloaded reference documents.

Standard-library tools need Python. Optional geometry and analysis modules import separately installed packages listed in the requirements files. Those packages retain their own licenses; importing a package does not place its source code in this repository. Verify the licenses of the exact versions you distribute or deploy.

The repository owner must confirm ownership and choose a license for this toolkit before describing it as open source. No license has been assigned by this preparation task.

VASP, VASPKIT and licensed potential datasets are obtained and used separately under their applicable terms. This candidate does not grant access to them and does not supply a configured remote backend.

The requirements files name dependencies rather than reproducing a tested installation lock. Deployments should lock and validate their own environments. In particular, MacroDensity availability and distribution differ between environments; obtain it from an authorized upstream source if using that optional module.
