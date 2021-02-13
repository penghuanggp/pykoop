import sklearn.base
import sklearn.utils.validation
import picos
import numpy as np
from scipy import linalg


class LmiEdmd(sklearn.base.BaseEstimator, sklearn.base.RegressorMixin):

    # TODO: The real number of minimum samples is based on the data. But this
    # value shuts up pytest for now.
    _check_X_y_params = {
        'multi_output': True,
        'y_numeric': True,
        'dtype': 'float64',
        'ensure_min_samples': 2,
    }

    def __init__(self, solver='mosek', inv_method='eig', picos_eps=1e-9):
        self.solver = solver
        self.inv_method = inv_method
        self.picos_eps = picos_eps

    def fit(self, X, y):
        self._validate_parameters()
        X, y = self._validate_data(X, y, reset=True, **self._check_X_y_params)
        problem = self._base_problem(X, y)
        problem.solve(solver=self.solver)
        self.U_ = self._extract_solution(problem)
        return self

    def predict(self, X):
        X = self._validate_data(X, reset=False)
        sklearn.utils.validation.check_is_fitted(self)
        Psi = X.T
        Theta_p = self.U_.T @ Psi
        return Theta_p.T

    def _validate_parameters(self):
        # Validate solver
        valid_solvers = [None, 'mosek']
        if self.solver not in valid_solvers:
            raise ValueError('`solver` must be one of: '
                             f'{" ".join(valid_solvers)}.')
        # Validate inverse method
        valid_inv_methods = ['inv', 'eig', 'ldl', 'chol', 'sqrt']
        if self.inv_method not in valid_inv_methods:
            raise ValueError('`inv_method` must be one of: '
                             f'{" ".join(valid_inv_methods)}.')

    def _validate_data(self, X, y=None, reset=True, **check_array_params):
        if y is None:
            X = sklearn.utils.validation.check_array(X, **check_array_params)
        else:
            X, y = sklearn.utils.validation.check_X_y(X, y,
                                                      **check_array_params)
        if reset:
            self.n_features_in_ = X.shape[1]
        return X if y is None else (X, y)

    def _base_problem(self, X, y):
        # Compute G and H
        Psi = X.T
        Theta_p = y.T
        q = Psi.shape[1]
        G = (Theta_p @ Psi.T) / q
        H = (Psi @ Psi.T) / q
        # Check rank of H
        rk = np.linalg.matrix_rank(H)
        if rk < H.shape[0]:
            # TODO Is it possible to use a pseudo-inverse here?
            # That would mean H could be rank deficient.
            # TODO Condition number warning? Log it?
            raise ValueError(
                'H must be full rank. '
                f'H is {H.shape[0]}x{X.shape[1]}. rk(H)={rk}.'
                'TODO: Loosen this requirement with a '
                'psuedo-inverse?'
            )
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
            H_inv = picos.Constant('H^-1', linalg.inv(H))
            problem.add_constraint((
                (  Z &     U) //  # noqa: E2
                (U.T & H_inv)
            ) >> 0)
        elif self.inv_method == 'eig':
            lmb, V = linalg.eigh(H)
            VsqrtLmb = picos.Constant('(V Lambda^(1/2))',
                                      V @ np.diag(np.sqrt(lmb)))
            problem.add_constraint((
                (               Z &       U * VsqrtLmb) //  # noqa: E2
                (VsqrtLmb.T * U.T & np.eye(U.shape[1]))     # noqa: E2
            ) >> 0)
        elif self.inv_method == 'ldl':
            L, D, _ = linalg.ldl(H)
            LsqrtD = picos.Constant('(L D^(1/2))', L @ np.sqrt(D))
            problem.add_constraint((
                (             Z &         U * LsqrtD) //  # noqa: E2
                (LsqrtD.T * U.T & np.eye(U.shape[1]))     # noqa: E2
            ) >> 0)
        elif self.inv_method == 'chol':
            L = picos.Constant('L', linalg.cholesky(H, lower=True))
            problem.add_constraint((
                (        Z &              U * L) //  # noqa: E2
                (L.T * U.T & np.eye(U.shape[1]))
            ) >> 0)
        elif self.inv_method == 'sqrt':
            # Since H is symmetric, its square root is symmetric.
            # Otherwise, this would not work!
            sqrtH = picos.Constant('sqrt(H)', linalg.sqrtm(H))
            problem.add_constraint((
                (            Z &          U * sqrtH) //  # noqa: E2
                (sqrtH.T * U.T & np.eye(U.shape[1]))
            ) >> 0)
        else:
            # Should never get here since input validation is done in `fit()`.
            raise ValueError('Invalid value for `inv_method`.')
        # Set objective
        problem.set_objective('min',
                              (-2 * picos.trace(U * G_T) + picos.trace(Z)))
        return problem

    def _extract_solution(self, problem):
        return np.array(problem.get_valued_variable('U')).T

    def _more_tags(self):
        return {
            'multioutput': True,
            'multioutput_only': True,
        }


class LmiEdmdTikhonov(LmiEdmd):

    def __init__(self, alpha=1.0, **kwargs):
        super().__init__(**kwargs)
        self.alpha = alpha

    def fit(self, X, y):
        # TODO Warn if alpha is zero?
        self._validate_parameters()
        X, y = self._validate_data(X, y, reset=True, **self._check_X_y_params)
        problem = self._base_problem(X, y)
        self._add_tikhonov(X, y, problem, self.alpha)
        problem.solve(solver=self.solver)
        self.U_ = self._extract_solution(problem)
        return self

    def _add_tikhonov(self, X, y, problem, alpha):
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
        problem.add_constraint((
            (picos.diag(gamma, p) & U.T) //
            (U & picos.diag(gamma, p_theta))
        ) >> 0)
        # Add term to cost function
        alpha_scaled = picos.Constant('alpha/q', alpha/q)
        objective += alpha_scaled * gamma**2
        problem.set_objective(direction, objective)
