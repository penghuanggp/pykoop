import sklearn.base
import sklearn.utils.validation
import picos
import numpy as np
from scipy import linalg
import logging
import tempfile
import joblib
import signal


# Create logger
log = logging.getLogger(__name__)


# Create temporary cache directory for memoized computations
cachedir = tempfile.TemporaryDirectory(prefix='pykoop_')
log.info(f'Temporary directory created at `{cachedir.name}`')
memory = joblib.Memory(cachedir.name, verbose=0)


# Create signal handler to politely stop computations
polite_stop = False

def sigint_handler(sig, frame):  # noqa: E302
    """Signal handler for ^C."""
    global polite_stop
    polite_stop = True

signal.signal(signal.SIGINT, sigint_handler)  # noqa: E305


class LmiEdmdTikhonovReg(sklearn.base.BaseEstimator,
                         sklearn.base.RegressorMixin):

    # Default solver parameters
    _default_solver_params = {
        'primals': None,
        'duals': None,
        'dualize': True,
        'abs_bnb_opt_tol': None,
        'abs_dual_fsb_tol': None,
        'abs_ipm_opt_tol': None,
        'abs_prim_fsb_tol': None,
        'integrality_tol': None,
        'markowitz_tol': None,
        'rel_bnb_opt_tol': None,
        'rel_dual_fsb_tol': None,
        'rel_ipm_opt_tol': None,
        'rel_prim_fsb_tol': None,
    }

    # The real number of minimum samples is based on the data. But this value
    # shuts up pytest for now. Picos also requires float64 so everything is
    # promoted.
    _check_X_y_params = {
        'multi_output': True,
        'y_numeric': True,
        'dtype': 'float64',
        'ensure_min_samples': 2,
    }

    def __init__(self, alpha=0.0, inv_method='chol', picos_eps=0,
                 solver_params=None):
        self.alpha = alpha
        self.inv_method = inv_method
        self.picos_eps = picos_eps
        self.solver_params = solver_params

    def fit(self, X, y, **kwargs):
        self._validate_parameters()
        X, y = self._validate_data(X, y, reset=True, **self._check_X_y_params)
        self.alpha_tikhonov_reg_ = self.alpha
        problem = self._get_base_problem(X, y)
        problem.solve(**self.solver_params_)
        self.solution_status_ = problem.last_solution.claimedStatus
        self.coef_ = self._extract_solution(problem)
        return self

    def predict(self, X):
        X = self._validate_data(X, reset=False)
        sklearn.utils.validation.check_is_fitted(self)
        Psi = X.T
        Theta_p = self.coef_.T @ Psi
        return Theta_p.T

    def _validate_parameters(self):
        # Validate alpha
        if self.alpha < 0:
            raise ValueError('`alpha` must be greater than zero.')
        # Validate inverse method
        valid_inv_methods = ['inv', 'pinv', 'eig', 'ldl', 'chol', 'sqrt']
        if self.inv_method not in valid_inv_methods:
            raise ValueError('`inv_method` must be one of: '
                             f'{", ".join(valid_inv_methods)}.')
        # Set solver params
        self.solver_params_ = self._default_solver_params.copy()
        if self.solver_params is not None:
            self.solver_params_.update(self.solver_params)

    def _validate_data(self, X, y=None, reset=True, **check_array_params):
        if y is None:
            X = sklearn.utils.validation.check_array(X, **check_array_params)
        else:
            X, y = sklearn.utils.validation.check_X_y(X, y,
                                                      **check_array_params)
        if reset:
            self.n_features_in_ = X.shape[1]
        return X if y is None else (X, y)

    def _get_base_problem(self, X, y):
        c, G, H, _ = _calc_c_G_H(X, y, self.alpha_tikhonov_reg_)
        # Optimization problem
        problem = picos.Problem()
        # Constants
        G_T = picos.Constant('G^T', G.T)
        # Variables
        U = picos.RealVariable('U', (G.shape[0], H.shape[0]))
        Z = picos.SymmetricVariable('Z', (G.shape[0], G.shape[0]))
        # Constraints
        problem.add_constraint(Z >> self.picos_eps)
        # Choose method to handle inverse of H
        if self.inv_method == 'inv':
            H_inv = picos.Constant('H^-1', _calc_Hinv(H))
            problem.add_constraint(picos.block([
                [  Z,     U],  # noqa: E201
                [U.T, H_inv]
            ]) >> 0)
        elif self.inv_method == 'pinv':
            H_inv = picos.Constant('H^+', _calc_Hpinv(H))
            problem.add_constraint(picos.block([
                [  Z,     U],  # noqa: E201
                [U.T, H_inv]
            ]) >> 0)
        elif self.inv_method == 'eig':
            VsqrtLmb = picos.Constant('(V Lambda^(1/2))', _calc_VsqrtLmb(H))
            problem.add_constraint(picos.block([
                [               Z, U * VsqrtLmb],  # noqa: E201
                [VsqrtLmb.T * U.T,          'I']
            ]) >> 0)
        elif self.inv_method == 'ldl':
            LsqrtD = picos.Constant('(L D^(1/2))', _calc_LsqrtD(H))
            problem.add_constraint(picos.block([
                [             Z, U * LsqrtD],  # noqa: E201
                [LsqrtD.T * U.T,        'I']
            ]) >> 0)
        elif self.inv_method == 'chol':
            L = picos.Constant('L', _calc_L(H))
            problem.add_constraint(picos.block([
                [        Z, U * L],  # noqa: E201
                [L.T * U.T,   'I']
            ]) >> 0)
        elif self.inv_method == 'sqrt':
            sqrtH = picos.Constant('sqrt(H)', _calc_sqrtH(H))
            problem.add_constraint(picos.block([
                [            Z, U * sqrtH],  # noqa: E201
                [sqrtH.T * U.T,       'I']
            ]) >> 0)
        else:
            # Should never get here since input validation is done in `fit()`.
            raise ValueError('Invalid value for `inv_method`.')
        # Set objective
        obj = c - 2 * picos.trace(U * G_T) + picos.trace(Z)
        problem.set_objective('min', obj)
        return problem

    def _extract_solution(self, problem):
        return np.array(problem.get_valued_variable('U'), ndmin=2).T

    def _more_tags(self):
        return {
            'multioutput': True,
            'multioutput_only': True,
        }


class LmiEdmdTikhonovRegSvd(LmiEdmdTikhonovReg):

    def fit(self, X, y, **kwargs):
        self.Q_X_, s_X, self.Vt_X_ = linalg.svd(X.T, full_matrices=False)
        self.Q_y_, s_y, self.Vt_y_ = linalg.svd(y.T, full_matrices=False)
        self.S_X_ = np.diag(s_X)
        self.Si_X_ = np.diag(1/s_X)
        self.S_y_ = np.diag(s_y)
        self.Si_y_ = np.diag(1/s_y)
        super().fit(X, y, **kwargs)

    def _get_base_problem(self, X, y):
        p = X.shape[1]
        p_theta = y.shape[1]
        problem = picos.Problem()
        V_hat = picos.Constant('V_hat', self.Vt_X_ @ self.Vt_y_.T)
        U_hat = picos.RealVariable('U_hat', (p_theta, p))
        Z = picos.SymmetricVariable('Z', (p_theta, p_theta))
        problem.add_constraint(picos.block([
            [np.eye(p_theta) - Z, U_hat],
            [U_hat.T, -np.eye(p)],
        ]) << self.picos_eps)
        problem.set_objective('min', picos.trace(Z - 2 * U_hat * V_hat))
        return problem

    def _extract_solution(self, problem):
        U_hat = np.array(problem.get_valued_variable('U_hat'), ndmin=2)
        U = self.S_y_ @ self.Q_y_ @ U_hat @ self.Q_X_.T @ self.Si_X_
        return U.T


class LmiEdmdTwoNormReg(LmiEdmdTikhonovReg):

    def __init__(self, alpha=1.0, ratio=1.0, inv_method='chol', picos_eps=0,
                 solver_params=None):
        self.alpha = alpha
        self.ratio = ratio
        self.inv_method = inv_method
        self.picos_eps = picos_eps
        self.solver_params = solver_params

    def fit(self, X, y, **kwargs):
        self._validate_parameters()
        X, y = self._validate_data(X, y, reset=True, **self._check_X_y_params)
        self.alpha_tikhonov_reg_ = self.alpha * (1 - self.ratio)
        self.alpha_other_reg_ = self.alpha * self.ratio
        problem = self._get_base_problem(X, y)
        self._add_twonorm(X, y, problem)
        problem.solve(**self.solver_params_)
        self.solution_status_ = problem.last_solution.claimedStatus
        self.coef_ = self._extract_solution(problem)
        return self

    def _add_twonorm(self, X, y, problem):
        # Extract information from problem
        U = problem.variables['U']
        direction = problem.objective.direction
        objective = problem.objective.function
        # Get needed sizes
        p_theta = U.shape[0]
        p = U.shape[1]
        q = X.shape[0]
        # Add new constraint
        gamma = picos.RealVariable('gamma', 1)
        problem.add_constraint(picos.block([
            [picos.diag(gamma, p),                        U.T],
            [                   U, picos.diag(gamma, p_theta)]  # noqa: E201
        ]) >> 0)
        # Add term to cost function
        alpha_scaled = picos.Constant('alpha_2/q', self.alpha_other_reg_/q)
        objective += alpha_scaled * gamma
        problem.set_objective(direction, objective)

    def _validate_parameters(self):
        if self.alpha == 0:
            raise ValueError('Value of `alpha` should not be zero. Use '
                             '`LmiEdmdTikhonovReg()` if you want to disable '
                             'regularization.')
        if self.ratio == 0:
            raise ValueError('Value of `ratio` should not be zero. Use '
                             '`LmiEdmdTikhonovReg()` if you want to disable '
                             'regularization.')
        super()._validate_parameters()


class LmiEdmdNuclearNormReg(LmiEdmdTikhonovReg):

    def __init__(self, alpha=1.0, ratio=1.0, inv_method='chol', picos_eps=0,
                 solver_params=None):
        self.alpha = alpha
        self.ratio = ratio
        self.inv_method = inv_method
        self.picos_eps = picos_eps
        self.solver_params = solver_params

    def fit(self, X, y, **kwargs):
        self._validate_parameters()
        X, y = self._validate_data(X, y, reset=True, **self._check_X_y_params)
        self.alpha_tikhonov_reg_ = self.alpha * (1 - self.ratio)
        self.alpha_other_reg_ = self.alpha * self.ratio
        problem = self._get_base_problem(X, y)
        self._add_nuclear(X, y, problem)
        problem.solve(**self.solver_params_)
        self.solution_status_ = problem.last_solution.claimedStatus
        self.coef_ = self._extract_solution(problem)
        return self

    def _add_nuclear(self, X, y, problem):
        # Extract information from problem
        U = problem.variables['U']
        direction = problem.objective.direction
        objective = problem.objective.function
        # Get needed sizes
        p_theta = U.shape[0]
        p = U.shape[1]
        q = X.shape[0]
        # Add new constraint
        gamma = picos.RealVariable('gamma', 1)
        W_1 = picos.SymmetricVariable('W_1', (p_theta, p_theta))
        W_2 = picos.SymmetricVariable('W_2', (p, p))
        problem.add_constraint(picos.trace(W_1) + picos.trace(W_2)
                               <= 2 * gamma)
        problem.add_constraint(picos.block([
            [W_1,   U],
            [U.T, W_2]
        ]) >> 0)
        # Add term to cost function
        alpha_scaled = picos.Constant('alpha_*/q', self.alpha_other_reg_/q)
        objective += alpha_scaled * gamma
        problem.set_objective(direction, objective)

    def _validate_parameters(self):
        if self.alpha == 0:
            raise ValueError('Value of `alpha` should not be zero. Use '
                             '`LmiEdmdTikhonovReg()` if you want to disable '
                             'regularization.')
        if self.ratio == 0:
            raise ValueError('Value of `ratio` should not be zero. Use '
                             '`LmiEdmdTikhonovReg()` if you want to disable '
                             'regularization.')
        super()._validate_parameters()


class LmiEdmdSpectralRadiusConstr(LmiEdmdTikhonovReg):

    def __init__(self, rho_bar=1.0, alpha=0, max_iter=100, tol=1e-6,
                 inv_method='chol', picos_eps=0, solver_params=None):
        self.rho_bar = rho_bar
        self.alpha = alpha
        self.max_iter = max_iter
        self.tol = tol
        self.inv_method = inv_method
        self.picos_eps = picos_eps
        self.solver_params = solver_params

    def fit(self, X, y, **kwargs):
        self._validate_parameters()
        X, y = self._validate_data(X, y, reset=True, **self._check_X_y_params)
        self.alpha_tikhonov_reg_ = self.alpha
        # Get needed sizes
        p_theta = y.shape[1]
        p = X.shape[1]
        # Make initial guesses and iterate
        Gamma = np.eye(p_theta)
        U_prev = np.zeros((p_theta, p))
        # Set scope of other variables
        U = np.zeros((p_theta, p))
        P = np.zeros((p_theta, p_theta))
        difference = None
        for k in range(self.max_iter):
            # Formulate Problem A
            problem_a = self._get_problem_a(X, y, Gamma)
            # Solve Problem A
            if polite_stop:
                self.stop_reason_ = 'User requested stop.'
                log.warn(self.stop_reason_)
                break
            log.info(f'Solving problem A{k}')
            problem_a.solve(**self.solver_params_)
            solution_status_a = problem_a.last_solution.claimedStatus
            if solution_status_a != 'optimal':
                self.stop_reason_ = (
                    'Unable to solve `problem_a`. Used last valid `U`. '
                    f'Solution status: `{solution_status_a}`.'
                )
                log.warn(self.stop_reason_)
                break
            U = np.array(problem_a.get_valued_variable('U'), ndmin=2)
            P = np.array(problem_a.get_valued_variable('P'), ndmin=2)
            # Formulate Problem B
            problem_b = self._get_problem_b(X, y, U, P)
            # Solve Problem B
            if polite_stop:
                self.stop_reason_ = 'User requested stop.'
                log.warn(self.stop_reason_)
                break
            log.info(f'Solving problem B{k}')
            problem_b.solve(**self.solver_params_)
            solution_status_b = problem_b.last_solution.claimedStatus
            if solution_status_b != 'optimal':
                self.stop_reason_ = (
                    'Unable to solve `problem_b`. Used last valid `U`. '
                    f'Solution status: `{solution_status_b}`.'
                )
                log.warn(self.stop_reason_)
                break
            Gamma = np.array(problem_b.get_valued_variable('Gamma'), ndmin=2)
            # Check stopping condition
            difference = _fast_frob_norm(U_prev - U)
            if (difference < self.tol):
                self.stop_reason_ = f'Reached tolerance {self.tol}'
                break
            U_prev = U
        else:
            self.stop_reason_ = f'Reached maximum iterations {self.max_iter}'
            log.warn(self.stop_reason_)
        self.tol_reached_ = difference
        self.n_iter_ = k + 1
        self.coef_ = U.T
        # Only useful for debugging
        self.Gamma_ = Gamma
        self.P_ = P
        return self

    def _get_problem_a(self, X, y, Gamma):
        problem_a = self._get_base_problem(X, y)
        # Extract information from problem
        U = problem_a.variables['U']
        # Get needed sizes
        p_theta = U.shape[0]
        # Add new constraints
        rho_bar_sq = picos.Constant('rho_bar^2', self.rho_bar**2)
        Gamma = picos.Constant('Gamma', Gamma)
        P = picos.SymmetricVariable('P', p_theta)
        problem_a.add_constraint(P >> self.picos_eps)
        problem_a.add_constraint(picos.block([
            [          rho_bar_sq * P, U[:, :p_theta].T * Gamma],  # noqa: E201
            [Gamma.T * U[:, :p_theta],      Gamma + Gamma.T - P]
        ]) >> self.picos_eps)
        return problem_a

    def _get_problem_b(self, X, y, U, P):
        # Create optimization problem
        problem_b = picos.Problem()
        # Get needed sizes
        p_theta = U.shape[0]
        # Create constants
        rho_bar_sq = picos.Constant('rho_bar^2', self.rho_bar**2)
        U = picos.Constant('U', U)
        P = picos.Constant('P', P)
        # Create variables
        Gamma = picos.RealVariable('Gamma', P.shape)
        # Add constraints
        problem_b.add_constraint(picos.block([
            [          rho_bar_sq * P, U[:, :p_theta].T * Gamma],  # noqa: E201
            [Gamma.T * U[:, :p_theta],      Gamma + Gamma.T - P]
        ]) >> self.picos_eps)
        # Set objective
        problem_b.set_objective('find')
        return problem_b


class LmiEdmdSpectralRadiusConstrIco(LmiEdmdTikhonovReg):

    def __init__(self, rho_bar=1.0, alpha=0, max_iter=100, tol=1e-6,
                 inv_method='chol', picos_eps=0, solver_params=None):
        self.rho_bar = rho_bar
        self.alpha = alpha
        self.max_iter = max_iter
        self.tol = tol
        self.inv_method = inv_method
        self.picos_eps = picos_eps
        self.solver_params = solver_params

    def fit(self, X, y, **kwargs):
        self._validate_parameters()
        X, y = self._validate_data(X, y, reset=True, **self._check_X_y_params)
        self.alpha_tikhonov_reg_ = self.alpha
        # Get needed sizes
        p_theta = y.shape[1]
        p = X.shape[1]
        # Make initial guess
        R, S = self._initial_guess(X, y)
        U = np.zeros((p_theta, p))
        U_prev = np.zeros((p_theta, p))
        difference = None
        for k in range(self.max_iter):
            problem = self._get_problem(X, y, R, S)
            if polite_stop:
                self.stop_reason_ = 'User requested stop.'
                log.warn(self.stop_reason_)
                break
            log.info(f'Solving problem {k}')
            problem.solve(**self.solver_params_)
            solution_status = problem.last_solution.claimedStatus
            if solution_status != 'optimal':
                self.stop_reason_ = (
                    'Unable to solve `problem`. Used last valid `U`. '
                    f'Solution status: `{solution_status}`.'
                )
                log.warn(self.stop_reason_)
                break
            U = np.array(problem.get_valued_variable('U'), ndmin=2)
            P = np.array(problem.get_valued_variable('P'), ndmin=2)
            R = self._R(U)
            S = self._S(P)
            # Check stopping condition
            difference = _fast_frob_norm(U_prev - U)
            if (difference < self.tol):
                self.stop_reason_ = f'Reached tolerance {self.tol}'
                break
            U_prev = U
        else:
            self.stop_reason_ = f'Reached maximum iterations {self.max_iter}'
            log.warn(self.stop_reason_)
        self.tol_reached_ = difference
        self.n_iter_ = k + 1
        self.coef_ = U.T

    def _R(self, U):
        A = U[:, :U.shape[0]]
        R = np.block([
            [np.eye(A.shape[0]),                  A],
            [ np.zeros(A.shape), np.eye(A.shape[0])],  # noqa: E201
        ])
        return R

    def _S(self, P):
        S = 0.5 * self.rho_bar * np.block([
            [                P, np.zeros(P.shape)],  # noqa: E201
            [np.zeros(P.shape),                 P],
        ])
        return S

    def _initial_guess(self, X, y):
        # Get stable A
        _, G, H, _ = _calc_c_G_H(X, y, 0)
        U_un = linalg.lstsq(H.T, G.T)[0].T
        A_un = U_un[:, :U_un.shape[0]]
        lmb, V = linalg.eig(A_un)
        for k in range(lmb.shape[0]):
            if np.absolute(lmb[k]) >= self.rho_bar:
                lmb[k] = (lmb[k] / np.absolute(lmb[k])) * self.rho_bar
                lmb[k] = lmb[k] / (1 + 1e-1)
        Lmb = np.diag(lmb)
        A_norm = np.real(linalg.solve(V.T, Lmb @ V.T).T)
        # Find feasible P
        problem = picos.Problem()
        p_theta = A_norm.shape[0]
        A = picos.Constant('A', A_norm)
        rho_bar = picos.Constant('rho_bar', self.rho_bar)
        P = picos.SymmetricVariable('P', (p_theta, p_theta))
        problem.add_constraint(P >> self.picos_eps)
        problem.add_constraint(picos.block([
            [rho_bar*P,       A*P],
            [  P.T*A.T, rho_bar*P],  # noqa: E201
        ]) >> 0)
        problem.set_objective('min', picos.trace(P))
        problem.solve(**self.solver_params_)
        P_0 = np.array(problem.get_valued_variable('P'), ndmin=2)
        # Form R and S
        R = np.block([
            [np.eye(A_norm.shape[0]),                  A_norm],
            [ np.zeros(A_norm.shape), np.eye(A_norm.shape[0])],  # noqa: E201
        ])
        S = 0.5 * self.rho_bar * np.block([
            [                P_0, np.zeros(P_0.shape)],  # noqa: E201
            [np.zeros(P_0.shape),                 P_0],
        ])
        return (R, S)


    def _get_problem(self, X, y, R_0, S_0):
        problem = self._get_base_problem(X, y)
        # Extract information from problem
        U = problem.variables['U']
        # Get needed sizes
        p_theta = U.shape[0]
        # Add new constraints
        rho_bar = picos.Constant('rho_bar', self.rho_bar)
        R_0 = picos.Constant('R_0', R_0)
        S_0 = picos.Constant('S_0', S_0)
        W = picos.Constant('W', 1e-2*np.eye(R_0.shape[1]))
        P = picos.SymmetricVariable('P', (p_theta, p_theta))
        problem.add_constraint(P >> self.picos_eps)
        A = U[:, :p_theta]
        R = picos.block([
            ['I', A],
            [0, 'I'],
        ], shapes=((p_theta, p_theta), (p_theta, p_theta)))
        S = picos.block([
            [0.5 * self.rho_bar * P, 0],  # noqa: E201
            [0, 0.5 * self.rho_bar * P],
        ])
        phi = (R * S_0) + (R_0 * S) - (R_0 * S_0)
        problem.add_constraint(picos.block([
            [(phi + phi.T), W.T * (R - R_0).T + (S - S_0)],
            [(R - R_0) * W + (S - S_0).T, -W],
        ]) << self.picos_eps)
        return problem


class LmiEdmdHinfReg(LmiEdmdTikhonovReg):

    def __init__(self, alpha=1.0, ratio=1.0, max_iter=100, tol=1e-6,
                 inv_method='chol', picos_eps=0, solver_params=None):
        self.alpha = alpha
        self.ratio = ratio
        self.max_iter = max_iter
        self.tol = tol
        self.inv_method = inv_method
        self.picos_eps = picos_eps
        self.solver_params = solver_params

    def fit(self, X, y, **kwargs):
        self._validate_parameters()
        X, y = self._validate_data(X, y, reset=True, **self._check_X_y_params)
        self.alpha_tikhonov_reg_ = self.alpha * (1 - self.ratio)
        self.alpha_other_reg_ = self.alpha * self.ratio
        # Get needed sizes
        p_theta = y.shape[1]
        p = X.shape[1]
        # Check that at least one input is present
        if p_theta == p:
            # If you remove the `{p} features(s)` part of this message,
            # the scikit-learn estimator_checks will fail!
            raise ValueError('LmiEdmdHinfReg() requires an input to function.'
                             '`X` and `y` must therefore have different '
                             'numbers of features. `X and y` both have '
                             f'{p} feature(s).')
        # Make initial guesses and iterate
        P_0 = kwargs.pop('P_0', None)
        if P_0 is None:
            P = np.eye(p_theta)
        elif P_0 == 'hot':
            P = self._hot_start(X, y)
        else:
            P = P_0
        U_prev = np.zeros((p_theta, p))
        # Set scope of other variables
        U = np.zeros((p_theta, p))
        gamma = 0
        difference = None
        for k in range(self.max_iter):
            # Formulate Problem A
            problem_a = self._get_problem_a(X, y, P)
            # Solve Problem A
            if polite_stop:
                self.stop_reason_ = 'User requested stop.'
                log.warn(self.stop_reason_)
                break
            log.info(f'Solving problem A{k}')
            problem_a.solve(**self.solver_params_)
            solution_status_a = problem_a.last_solution.claimedStatus
            if solution_status_a != 'optimal':
                self.stop_reason_ = (
                    'Unable to solve `problem_a`. Used last valid `U`. '
                    f'Solution status: `{solution_status_a}`.'
                )
                log.warn(self.stop_reason_)
                break
            U = np.array(problem_a.get_valued_variable('U'), ndmin=2)
            gamma = np.array(problem_a.get_valued_variable('gamma'))
            # Formulate Problem B
            problem_b = self._get_problem_b(X, y, U, gamma)
            # Solve Problem B
            if polite_stop:
                self.stop_reason_ = 'User requested stop.'
                log.warn(self.stop_reason_)
                break
            log.info(f'Solving problem B{k}')
            problem_b.solve(**self.solver_params_)
            solution_status_b = problem_b.last_solution.claimedStatus
            if solution_status_b != 'optimal':
                self.stop_reason_ = (
                    'Unable to solve `problem_b`. Used last valid `U`. '
                    'Solution status: f`{solution_status_b}`.'
                )
                log.warn(self.stop_reason_)
                break
            P = np.array(problem_b.get_valued_variable('P'), ndmin=2)
            # Check stopping condition
            difference = _fast_frob_norm(U_prev - U)
            if (difference < self.tol):
                self.stop_reason_ = f'Reached tolerance {self.tol}'
                break
            U_prev = U
        else:
            self.stop_reason_ = f'Reached maximum iterations {self.max_iter}'
            log.warn(self.stop_reason_)
        self.tol_reached_ = difference
        self.n_iter_ = k + 1
        self.coef_ = U.T
        # Only useful for debugging
        self.P_ = P
        self.gamma_ = gamma
        return self

    def _get_problem_a(self, X, y, P):
        problem_a = self._get_base_problem(X, y)
        # Extract information from problem
        U = problem_a.variables['U']
        direction = problem_a.objective.direction
        objective = problem_a.objective.function
        # Get needed sizes
        p_theta = U.shape[0]
        q = X.shape[0]
        # Add new constraint
        P = picos.Constant('P', P)
        gamma = picos.RealVariable('gamma', 1)
        A = U[:p_theta, :p_theta]
        B = U[:p_theta, p_theta:]
        C = np.eye(p_theta)
        D = np.zeros((C.shape[0], B.shape[1]))
        gamma_33 = picos.diag(gamma, D.shape[1])
        gamma_44 = picos.diag(gamma, D.shape[0])
        problem_a.add_constraint(picos.block([
            [      P,   A*P,        B,        0],  # noqa: E201
            [P.T*A.T,     P,        0,    P*C.T],
            [    B.T,     0, gamma_33,      D.T],  # noqa: E201
            [      0, C*P.T,        D, gamma_44]   # noqa: E201
        ]) >> self.picos_eps)
        # Add term to cost function
        alpha_scaled = picos.Constant('alpha_inf/q', self.alpha_other_reg_/q)
        objective += alpha_scaled * gamma
        problem_a.set_objective(direction, objective)
        return problem_a

    def _get_problem_b(self, X, y, U, gamma):
        # Create optimization problem
        problem_b = picos.Problem()
        # Get needed sizes
        p_theta = U.shape[0]
        # Create constants
        U = picos.Constant('U', U)
        gamma = picos.Constant('gamma', gamma)
        # Create variables
        P = picos.SymmetricVariable('P', p_theta)
        # Add constraints
        problem_b.add_constraint(P >> self.picos_eps)
        A = U[:p_theta, :p_theta]
        B = U[:p_theta, p_theta:]
        C = np.eye(p_theta)
        D = np.zeros((C.shape[0], B.shape[1]))
        gamma_33 = picos.diag(gamma, D.shape[1])
        gamma_44 = picos.diag(gamma, D.shape[0])
        problem_b.add_constraint(picos.block([
            [      P,   A*P,        B,        0],  # noqa: E201
            [P.T*A.T,     P,        0,    P*C.T],
            [    B.T,     0, gamma_33,      D.T],  # noqa: E201
            [      0, C*P.T,        D, gamma_44]   # noqa: E201
        ]) >> self.picos_eps)
        # Set objective
        problem_b.set_objective('find')
        return problem_b

    def _hot_start(self, X, y):
        log.info('Running `_hot_start()` to estimate P_0')
        _, G, H, _ = _calc_c_G_H(X, y, 0)
        U_un = linalg.lstsq(H.T, G.T)[0].T
        A_un = U_un[:, :U_un.shape[0]]
        B_un = U_un[:, U_un.shape[0]:]
        lmb, V = linalg.eig(A_un)
        for k in range(lmb.shape[0]):
            if np.absolute(lmb[k]) >= 1:
                lmb[k] = lmb[k] / np.absolute(lmb[k]) / 1.1
        Lmb = np.diag(lmb)
        A_norm = np.real(V @ Lmb @ linalg.inv(V))
        U_norm = np.hstack((A_norm, B_un))

        problem = picos.Problem()
        p_theta = U_norm.shape[0]
        U = picos.Constant('U_norm', U_norm)
        A = U[:p_theta, :p_theta]
        B = U[:p_theta, p_theta:]
        C = np.eye(p_theta)
        D = np.zeros((C.shape[0], B.shape[1]))
        gamma = picos.RealVariable('gamma', 1)
        P = picos.SymmetricVariable('P', p_theta)
        gamma_33 = picos.diag(gamma, D.shape[1])
        gamma_44 = picos.diag(gamma, D.shape[0])
        problem.add_constraint(P >> self.picos_eps)
        problem.add_constraint(picos.block([
            [      P,   A*P,        B,        0],  # noqa: E201
            [P.T*A.T,     P,        0,    P*C.T],
            [    B.T,     0, gamma_33,      D.T],  # noqa: E201
            [      0, C*P.T,        D, gamma_44]   # noqa: E201
        ]) >> self.picos_eps)
        problem.set_objective('min', gamma)
        problem.solve(**self.solver_params_)
        P_0 = np.array(problem.get_valued_variable('P'), ndmin=2)
        return P_0

    def _validate_parameters(self):
        if self.alpha == 0:
            raise ValueError('Value of `alpha` should not be zero. Use '
                             '`LmiEdmdTikhonovReg()` if you want to disable '
                             'regularization.')
        if self.ratio == 0:
            raise ValueError('Value of `ratio` should not be zero. Use '
                             '`LmiEdmdTikhonovReg()` if you want to disable '
                             'regularization.')
        super()._validate_parameters()


class LmiEdmdDissipativityConstr(LmiEdmdTikhonovReg):
    """See hara_2019_learning

    Supply rate:
        s(u, y) = -[y, u] Xi [y; u],
    where
        Xi = [0, -1; -1, 0] -> passivity,
        Xi = [1, 0; 0, gamma] -> bounded L2 gain of gamma.

    (Using MATLAB array notation here. To clean up and LaTeX)


    @article{hara_2019_learning,
        title={Learning Koopman Operator under Dissipativity Constraints},
        author={Keita Hara and Masaki Inoue and Noboru Sebe},
        year={2019},
        journaltitle={{\tt arXiv:1911.03884v1 [eess.SY]}}
    }

    Currently not fully tested!
    """

    def __init__(self, alpha=0.0, max_iter=100, tol=1e-6, inv_method='chol',
                 picos_eps=0, solver_params=None):
        self.alpha = 0
        self.max_iter = max_iter
        self.tol = tol
        self.inv_method = inv_method
        self.picos_eps = picos_eps
        self.solver_params = solver_params

    def fit(self, X, y, **kwargs):
        self._validate_parameters()
        X, y = self._validate_data(X, y, reset=True, **self._check_X_y_params)
        self.alpha_tikhonov_reg_ = self.alpha
        self.supply_rate_xi_ = kwargs['supply_rate_xi']
        # Get needed sizes
        p_theta = y.shape[1]
        p = X.shape[1]
        # Make initial guess and iterate
        P = np.eye(p_theta)
        U_prev = np.zeros((p_theta, p))
        # Set scope of other variables
        difference = None
        U = np.zeros((p_theta, p))
        for k in range(self.max_iter):
            # Formulate Problem A
            log.info(f'Solving problem A{k}')
            problem_a = self._get_problem_a(X, y, P)
            # Solve Problem A
            if polite_stop:
                self.stop_reason_ = 'User requested stop.'
                log.warn(self.stop_reason_)
                break
            problem_a.solve(**self.solver_params_)
            solution_status_a = problem_a.last_solution.claimedStatus
            if solution_status_a != 'optimal':
                self.stop_reason_ = (
                    'Unable to solve `problem_a`. Used last valid `U`. '
                    f'Solution status: `{solution_status_a}`.'
                )
                log.warn(self.stop_reason_)
                break
            U = np.array(problem_a.get_valued_variable('U'), ndmin=2)
            # Formulate Problem B
            problem_b = self._get_problem_b(X, y, U)
            # Solve Problem B
            if polite_stop:
                self.stop_reason_ = 'User requested stop.'
                log.warn(self.stop_reason_)
                break
            log.info(f'Solving problem B{k}')
            problem_b.solve(**self.solver_params_)
            solution_status_b = problem_b.last_solution.claimedStatus
            if solution_status_b != 'optimal':
                self.stop_reason_ = (
                    'Unable to solve `problem_b`. Used last valid `U`. '
                    f'Solution status: `{solution_status_b}`.'
                )
                log.warn(self.stop_reason_)
                break
            P = np.array(problem_b.get_valued_variable('P'), ndmin=2)
            # Check stopping condition
            difference = _fast_frob_norm(U_prev - U)
            if (difference < self.tol):
                self.stop_reason_ = f'Reached tolerance {self.tol}'
                break
            U_prev = U
        else:
            self.stop_reason_ = f'Reached maximum iterations {self.max_iter}'
            log.warn(self.stop_reason_)
        self.tol_reached_ = difference
        self.n_iter_ = k + 1
        self.coef_ = U.T
        # Only useful for debugging
        self.P_ = P
        return self

    def _get_problem_a(self, X, y, P):
        problem_a = self._get_base_problem(X, y)
        # Extract information from problem
        U = problem_a.variables['U']
        # Get needed sizes
        p_theta = U.shape[0]
        # Add new constraints
        P = picos.Constant('P', P)
        A = U[:, :p_theta]
        B = U[:, p_theta:]
        C = picos.Constant('C', np.eye(p_theta))
        Xi11 = picos.Constant('Xi_11',
                              self.supply_rate_xi_[:p_theta, :p_theta])
        Xi12 = picos.Constant('Xi_12',
                              self.supply_rate_xi_[:p_theta, p_theta:])
        Xi22 = picos.Constant('Xi_22',
                              self.supply_rate_xi_[p_theta:, p_theta:])
        problem_a.add_constraint(picos.block([
            [P - C.T*Xi11*C, -C.T*Xi12, A.T*P],
            [     -Xi12.T*C,     -Xi22, B.T*P],  # noqa: E201 E221
            [           P*A,       P*B,     P]   # noqa: E201
        ]) >> self.picos_eps)
        return problem_a

    def _get_problem_b(self, X, y, U):
        # Create optimization problem
        problem_b = picos.Problem()
        # Get needed sizes
        p_theta = U.shape[0]
        # Create constants
        U = picos.Constant('U', U)
        # Create variables
        P = picos.SymmetricVariable('P', p_theta)
        # Add constraints
        A = U[:, :p_theta]
        B = U[:, p_theta:]
        C = picos.Constant('C', np.eye(p_theta))
        Xi11 = picos.Constant('Xi_11',
                              self.supply_rate_xi_[:p_theta, :p_theta])
        Xi12 = picos.Constant('Xi_12',
                              self.supply_rate_xi_[:p_theta, p_theta:])
        Xi22 = picos.Constant('Xi_22',
                              self.supply_rate_xi_[p_theta:, p_theta:])
        problem_b.add_constraint(P >> self.picos_eps)
        problem_b.add_constraint(picos.block([
            [P - C.T*Xi11*C, -C.T*Xi12, A.T*P],
            [     -Xi12.T*C,     -Xi22, B.T*P],  # noqa: E201 E221
            [           P*A,       P*B,     P],  # noqa: E201 E222
        ]) >> self.picos_eps)
        # Set objective
        problem_b.set_objective('find')
        return problem_b


@memory.cache
def _calc_c_G_H(X, y, alpha):
    """Memoized computation of ``G`` and ``H``. If this function is called
    a second time with the same parameters, cached versions of ``G`` and
    ``H`` are returned.
    """
    # Compute G and H
    Psi = X.T
    Theta_p = y.T
    p = Psi.shape[0]
    q = Psi.shape[1]
    # Compute G and Tikhonov-regularized H
    G = (Theta_p @ Psi.T) / q
    H_unreg = (Psi @ Psi.T) / q
    H_reg = H_unreg + (alpha * np.eye(p)) / q
    # Compute c
    c = np.trace(Theta_p @ Theta_p.T) / q
    # Check condition number and rank of G and H
    cond_G = np.linalg.cond(G)
    rank_G = np.linalg.matrix_rank(G)
    shape_G = G.shape
    cond_H_unreg = np.linalg.cond(H_unreg)
    rank_H_unreg = np.linalg.matrix_rank(H_unreg)
    shape_H_unreg = H_unreg.shape
    cond_H_reg = np.linalg.cond(H_reg)
    rank_H_reg = np.linalg.matrix_rank(H_reg)
    shape_H_reg = H_reg.shape
    stats = {
        'cond_G': cond_G,
        'rank_G': rank_G,
        'shape_G': shape_G,
        'cond_H_unreg': cond_H_unreg,
        'rank_H_unreg': rank_H_unreg,
        'shape_H_unreg': shape_H_unreg,
        'cond_H_reg': cond_H_reg,
        'rank_H_reg': rank_H_reg,
        'shape_H_reg': shape_H_reg,
    }
    stats_str = {}
    for key in stats:
        if 'cond' in key:
            stats_str[key] = f'{stats[key]:.2e}'
        else:
            stats_str[key] = stats[key]
    log.info(f'_calc_c_G_H() stats: {stats_str}')
    return c, G, H_reg, stats


@memory.cache
def _calc_Hinv(H):
    return linalg.inv(H)


@memory.cache
def _calc_Hpinv(H):
    return linalg.pinv(H)


@memory.cache
def _calc_VsqrtLmb(H):
    lmb, V = linalg.eigh(H)
    return V @ np.diag(np.sqrt(lmb))


@memory.cache
def _calc_LsqrtD(H):
    L, D, _ = linalg.ldl(H)
    return L @ np.sqrt(D)


@memory.cache
def _calc_L(H):
    return linalg.cholesky(H, lower=True)


@memory.cache
def _calc_sqrtH(H):
    # Since H is symmetric, its square root is symmetric.
    # Otherwise, this would not work!
    return linalg.sqrtm(H)


def _fast_frob_norm(A):
    # Maybe this is premature optimization but scipy.linalg.norm() is
    # notoriously slow.
    return np.sqrt(np.trace(A @ A.T))
