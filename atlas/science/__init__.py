"""Science processing pipelines and submission formatters.

Phase 1 contains:
    submissions.base    Abstract Submitter
    submissions.mpc     MPC formatter (TODO Phase 2)
    submissions.aavso   AAVSO formatter (TODO Phase 2)
    submissions.tns     TNS formatter (TODO Phase 2)
    submissions.nasa_eo NASA Exoplanet Watch formatter (TODO Phase 2)

Phase 2 will add:
    plate_solve         ASTAP integration
    photometry          aperture/PSF photometry pipeline
    astrometry          centroid + Gaia DR3 reference
    subtraction         image subtraction for transient detection
"""
