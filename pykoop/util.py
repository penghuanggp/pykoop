import numpy as np
from scipy import signal, interpolate


def random_state(low, high, rng=None):
    """Generates a random initial state.

    Generates uniform random data between specified bounds.

    Very simply wrapper. Really only exists to keep a common interface with
    `random_input`, which is much more complex.

    Parameters
    ----------
    low : float or (n, 1) np.ndarray
        Lower bound for uniform random distribution.
    high : float or (n, 1) np.ndarray
        Upper bound for uniform random distribution.
    rng : Generator
        Random number generator, `numpy.random.default_rng(seed)`.

    Returns
    -------
    np.ndarray:
        Random initial state.

    """
    rng = np.random.default_rng()
    x_rand = rng.uniform(low, high, low.shape)
    return x_rand


def random_input(t_range, t_step, low, high, cutoff, order=2, rng=None,
                 output='function'):
    """Generates a smooth random input.

    Generates uniform random data between specified bounds, lowpass filters the
    data, then optionally linearly interpolates to return a function of time.

    Uses a Butterworth filter of specified order.

    Parameters
    ----------
    t_range : (2,) tuple
        Start and end times in a tuple (s).
    t_step : float
        Time step at which to generate random data (s).
    low : float or (n, 1) np.ndarray
        Lower bound for uniform random distribution.
    high : float or (n, 1) np.ndarray
        Upper bound for uniform random distribution.
    cutoff : float
        Cutoff frequency for Butterworth lowpass filter (Hz).
    order : int
        Order of Butterworth lowpass filter.
    rng : Generator
        Random number generator, `numpy.random.default_rng(seed)`.
    output : str
        Output format to use. Value 'array' causes the function to return an
        array of smoothed data. Value 'function' causes the function to return
        a function generated by linearly interpolating that same array.

    Returns
    -------
    function or np.ndarray :
        If `output` is 'function', returns a function representing
        linearly-interpolated lowpass-filtered uniformly-random data. If
        `output` is 'array', returns an array containing lowpass-filtered
        uniformly-random data. Units are same as `low` and `high`.
    """
    t = np.arange(*t_range, t_step)
    size = np.shape(low) + (t.shape[-1],)  # Concatenate tuples
    if rng is None:
        rng = np.random.default_rng()
    u_rough = rng.uniform(np.reshape(low, size[:-1] + (1,)),
                          np.reshape(high, size[:-1] + (1,)), size)
    sos = signal.butter(order, cutoff, output='sos', fs=1/t_step)
    u_smooth = signal.sosfilt(sos, u_rough)
    if output == 'array':
        return u_smooth
    elif output == 'function':
        f_smooth = interpolate.interp1d(t, u_smooth, fill_value='extrapolate')
        return f_smooth
    else:
        raise ValueError(f'{output} is not a valid output form.')
