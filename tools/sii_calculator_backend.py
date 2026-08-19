"""
Pure computation for the SII bunching-excess / integration-time calculator.

Combines two exploratory notebooks:
  - visibility_formalism.ipynb -- <|V|^2> for a Gaussian/uniform-disc source,
    single-aperture or two-aperture (baseline) case.
  - sii_snr.ipynb -- magnitude -> photon flux.

and the coherence-time relation already used in
.claude/skills/solve-wave/solve_wave.py (tc = lambda^2 / (c * dlambda)).

All functions work in SI units (meters, radians, seconds); unit conversion
(mm/nm/ps/mas) is the caller's responsibility.
"""
import numpy as np
from scipy.special import j1
from scipy.integrate import quad

C_LIGHT = 2.99792458e8  # m/s


# ---------------------------------------------------------------------
# Object visibility models V_obj(u)  -- copied from visibility_formalism.ipynb
# ---------------------------------------------------------------------
def uniform_disc_visibility(theta, u):
    """
    Visibility of a uniformly bright circular disc of angular diameter
    `theta` [rad], at spatial frequency `u` [1/rad]:  V(u) = 2 J1(x)/x,
    x = pi*theta*u.
    """
    u = np.asarray(u, dtype=float)
    x = np.pi * theta * u
    out = np.ones_like(x)
    nonzero = np.abs(x) > 1e-10
    out[nonzero] = 2.0 * j1(x[nonzero]) / x[nonzero]
    return out


def gaussian_visibility(mfd, u):
    """
    Visibility of a circularly symmetric Gaussian-profile source, specified
    by its angular MFD (1/e^2 full width) `mfd` [rad], at spatial frequency
    `u` [1/rad]:  V(u) = exp(-pi^2 * mfd^2 * u^2 / 8).
    """
    u = np.asarray(u, dtype=float)
    w0 = mfd / 2.0
    a = (np.pi ** 2 * w0 ** 2) / 2.0
    return np.exp(-a * u ** 2)


def fwhm_to_mfd(fwhm):
    """Convert Gaussian angular FWHM [rad] to MFD [rad] (1/e^2 full width)."""
    return fwhm * np.sqrt(2.0 / np.log(2.0))


def mfd_to_fwhm(mfd):
    """Convert Gaussian angular MFD [rad] (1/e^2 full width) to FWHM [rad]."""
    return mfd * np.sqrt(np.log(2.0) / 2.0)


# ---------------------------------------------------------------------
# Pupil autocorrelation (instrument transfer function)
# ---------------------------------------------------------------------
def aperture_autocorr(u, D, lam):
    """
    Normalized autocorrelation of a single circular aperture of diameter D,
    A(u) = overlap area of two D-diameter circles displaced by u*lam,
    normalized to A(0)=1. Zero for |u| > D/lam.
    """
    u = np.asarray(u, dtype=float)
    r = np.abs(u) * lam / D
    r = np.clip(r, 0.0, 1.0)
    val = (2.0 / np.pi) * (np.arccos(r) - r * np.sqrt(1.0 - r ** 2))
    return np.where(np.abs(u) * lam / D <= 1.0, val, 0.0)


def effective_V2(vis_func, D, lam, baseline=0.0, sub_aperture_D=None):
    """
    Aperture(-pair)-weighted average of |V_obj(u)|^2:

        <|V|^2> = Integral[ |V_obj(u)|^2 * A(u - u_c) du ]
                  ----------------------------------------
                       Integral[ A(u - u_c) du ]

    baseline=0 -> single aperture, lobe centered at u=0.
    baseline>0 -> two-aperture interferometer, fringe side-lobe centered at
    u_c = baseline/lam, sub-apertures of diameter sub_aperture_D (default D).
    """
    if sub_aperture_D is None:
        sub_aperture_D = D

    if baseline == 0.0:
        u_c = 0.0
        half_width = D / lam
        lo, hi = 0.0, half_width
    else:
        u_c = baseline / lam
        half_width = sub_aperture_D / lam
        lo, hi = u_c - half_width, u_c + half_width

    def integrand_num(u):
        return vis_func(u) ** 2 * aperture_autocorr(u - u_c, sub_aperture_D, lam)

    def integrand_den(u):
        return aperture_autocorr(u - u_c, sub_aperture_D, lam)

    num, _ = quad(integrand_num, lo, hi, limit=200)
    den, _ = quad(integrand_den, lo, hi, limit=200)
    return num / den


# ---------------------------------------------------------------------
# Coherence time -- same relation as solve_wave.py:608-611
#   tau_c = lam**2 / (C_LIGHT * dlam)
# ---------------------------------------------------------------------
def coherence_time(lam0, dlambda):
    """tc [s] = lam0^2 / (C_LIGHT * dlambda), lam0 and dlambda in meters."""
    return lam0 ** 2 / (C_LIGHT * dlambda)


# ---------------------------------------------------------------------
# Bunching excess
# ---------------------------------------------------------------------
def bunching_R(V2, tc, td):
    """R = 0.5 * <|V|^2> * tc / td (dimensionless)."""
    return 0.5 * V2 * tc / td


def bunching_excess(R, avg_coincidence):
    """bunching_bin = R * avg_coincidence."""
    return R * avg_coincidence


# ---------------------------------------------------------------------
# SNR / integration time
#   snr = ndot * R * (T * td)**0.5
#   ->  T = (target_snr / (R * ndot))**2 / td
# ---------------------------------------------------------------------
def snr_for_time(ndot, R, T, td):
    return ndot * R * (T * td) ** 0.5


def required_time(target_snr, R, ndot, td):
    return (target_snr / (R * ndot)) ** 2 / td


# ---------------------------------------------------------------------
# Photon rate from magnitude -- magnitude_to_flux copied from sii_snr.ipynb
# ---------------------------------------------------------------------
def magnitude_to_flux(mv, mv0=0.0, nv0=9e-5):
    """
    Photon flux nv [photons m^-2 s^-1 Hz^-1] at apparent magnitude mv.
    nv0 is the flux at reference magnitude mv0 (default 9e-5).
    """
    return nv0 * 10 ** (-0.4 * (mv - mv0))


def ndot_from_magnitude(mv, tc, D, alpha=0.2, eta=0.2, mv0=0.0, nv0=9e-5):
    """
    Photon count rate [counts/s] from apparent magnitude, reusing the same
    aperture diameter D [m] used for <|V|^2> and the same coherence time tc
    [s] used for R (bandwidth dnu = 1/tc) rather than re-entering either.

        ndot = flux(mv) * (1/tc) * (pi*(D/2)**2) * alpha * eta
    """
    flux = magnitude_to_flux(mv, mv0, nv0)
    dnu = 1.0 / tc
    area = np.pi * (D / 2.0) ** 2
    return flux * dnu * area * alpha * eta
