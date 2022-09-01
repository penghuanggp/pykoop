"""Test :mod:`lmi_regressors`."""

import numpy as np
import pytest
import sklearn.utils.estimator_checks
from scipy import signal

import pykoop
from pykoop import lmi_regressors


@pytest.fixture
def mosek_solver_params(remote, remote_url):
    """MOSEK solver parameters."""
    params = {
        'solver': 'mosek',
        'dualize': True,
        '*_fsb_tol': 1e-6,
        '*_opt_tol': 1e-6,
    }
    # Set MOSEK solver to remote server if needed
    if remote:
        params['mosek_server'] = remote_url
    return params


@pytest.mark.mosek
@pytest.mark.parametrize(
    'class_',
    [
        pykoop.lmi_regressors.LmiEdmdHinfReg,
        pykoop.lmi_regressors.LmiDmdcHinfReg,
    ],
)
class TestLmiHinfZpkMeta:
    """Test :class:`LmiHinfZpkMeta`."""

    def test_hinf_zpk_meta(
        self,
        class_,
        mosek_solver_params,
        mass_spring_damper_sine_input,
    ):
        """Test that :class:`LmiHinfZpkMeta` weight is correct.

        .. todo:: Break up multiple asserts.
        """
        # Compute state space matrices
        ss_ct = signal.ZerosPolesGain(
            [-0],
            [-5],
            1,
        ).to_ss()
        ss_dt = ss_ct.to_discrete(
            dt=mass_spring_damper_sine_input['t_step'],
            method='bilinear',
        )
        weight = (
            'post',
            ss_dt.A,
            ss_dt.B,
            ss_dt.C,
            ss_dt.D,
        )
        est_expected = class_(weight=weight, solver_params=mosek_solver_params)
        est_actual = pykoop.lmi_regressors.LmiHinfZpkMeta(
            hinf_regressor=class_(solver_params=mosek_solver_params),
            type='post',
            zeros=-0,
            poles=-5,
            gain=1,
            discretization='bilinear',
            t_step=mass_spring_damper_sine_input['t_step'],
        )
        # Fit regressors
        est_expected.fit(
            mass_spring_damper_sine_input['X_train'],
            n_inputs=mass_spring_damper_sine_input['n_inputs'],
            episode_feature=mass_spring_damper_sine_input['episode_feature'],
        )
        est_actual.fit(
            mass_spring_damper_sine_input['X_train'],
            n_inputs=mass_spring_damper_sine_input['n_inputs'],
            episode_feature=mass_spring_damper_sine_input['episode_feature'],
        )
        # Check Koopman matrices
        U_expected = est_expected.coef_.T
        U_actual = est_actual.hinf_regressor_.coef_.T
        np.testing.assert_allclose(U_actual, U_expected)
        # Check state space matrices
        assert est_expected.weight[0] == est_actual.hinf_regressor_.weight[0]
        for i in range(1, 5):
            np.testing.assert_allclose(
                est_expected.weight[i],
                est_actual.hinf_regressor_.weight[i],
            )

    def test_hinf_zpk_units(
        self,
        class_,
        mosek_solver_params,
        mass_spring_damper_sine_input,
    ):
        """Test that :class:`LmiHinfZpkMeta` zero and pole units are correct.

        .. todo:: Break up multiple asserts.
        """
        est_1 = pykoop.lmi_regressors.LmiHinfZpkMeta(
            hinf_regressor=class_(solver_params=mosek_solver_params),
            type='post',
            zeros=-0,
            poles=(-2 * np.pi / mass_spring_damper_sine_input['t_step']) / 2,
            gain=1,
            discretization='bilinear',
            t_step=mass_spring_damper_sine_input['t_step'],
            units='rad/s',
        )
        est_2 = pykoop.lmi_regressors.LmiHinfZpkMeta(
            hinf_regressor=class_(solver_params=mosek_solver_params),
            type='post',
            zeros=-0,
            poles=(-1 / mass_spring_damper_sine_input['t_step']) / 2,
            gain=1,
            discretization='bilinear',
            t_step=mass_spring_damper_sine_input['t_step'],
            units='hz',
        )
        est_3 = pykoop.lmi_regressors.LmiHinfZpkMeta(
            hinf_regressor=class_(solver_params=mosek_solver_params),
            type='post',
            zeros=-0,
            poles=-1,
            gain=1,
            discretization='bilinear',
            t_step=mass_spring_damper_sine_input['t_step'],
            units='normalized',
        )
        # Fit estimators
        est_1.fit(
            mass_spring_damper_sine_input['X_train'],
            n_inputs=mass_spring_damper_sine_input['n_inputs'],
            episode_feature=mass_spring_damper_sine_input['episode_feature'],
        )
        est_2.fit(
            mass_spring_damper_sine_input['X_train'],
            n_inputs=mass_spring_damper_sine_input['n_inputs'],
            episode_feature=mass_spring_damper_sine_input['episode_feature'],
        )
        est_3.fit(
            mass_spring_damper_sine_input['X_train'],
            n_inputs=mass_spring_damper_sine_input['n_inputs'],
            episode_feature=mass_spring_damper_sine_input['episode_feature'],
        )
        # Check poles
        np.testing.assert_allclose(est_1.ss_ct_.poles, est_2.ss_ct_.poles)
        np.testing.assert_allclose(est_2.ss_ct_.poles, est_3.ss_ct_.poles)
        np.testing.assert_allclose(est_3.ss_ct_.poles, est_1.ss_ct_.poles)
        # Check zeros
        np.testing.assert_allclose(est_1.ss_ct_.zeros, est_2.ss_ct_.zeros)
        np.testing.assert_allclose(est_2.ss_ct_.zeros, est_3.ss_ct_.zeros)
        np.testing.assert_allclose(est_3.ss_ct_.zeros, est_1.ss_ct_.zeros)
        # Check parameters
        assert est_1.n_features_in_ == est_1.hinf_regressor_.n_features_in_
        assert est_1.n_states_in_ == est_1.hinf_regressor_.n_states_in_
        assert est_1.n_inputs_in_ == est_1.hinf_regressor_.n_inputs_in_
        assert est_1.episode_feature_ == est_1.hinf_regressor_.episode_feature_
        np.testing.assert_allclose(est_1.coef_, est_1.hinf_regressor_.coef_)


@pytest.mark.mosek
class TestSklearn:
    """Test scikit-learn compatibility."""

    @sklearn.utils.estimator_checks.parametrize_with_checks([
        pykoop.lmi_regressors.LmiEdmd(alpha=1e-3, ),
        pykoop.lmi_regressors.LmiEdmdSpectralRadiusConstr(max_iter=1, ),
        pykoop.lmi_regressors.LmiEdmdHinfReg(alpha=1, ratio=1, max_iter=1),
        pykoop.lmi_regressors.LmiEdmdDissipativityConstr(max_iter=1, ),
        pykoop.lmi_regressors.LmiDmdc(alpha=1e-3, ),
        pykoop.lmi_regressors.LmiDmdcSpectralRadiusConstr(max_iter=1, ),
        pykoop.lmi_regressors.LmiDmdcHinfReg(alpha=1, ratio=1, max_iter=1),
        pykoop.lmi_regressors.LmiHinfZpkMeta(
            pykoop.lmi_regressors.LmiEdmdHinfReg(
                alpha=1,
                ratio=1,
                max_iter=1,
            )),
    ])
    def test_compatible_estimator(self, estimator, check, mosek_solver_params):
        """Test scikit-learn compatibility for LMI-based regressors."""
        if hasattr(estimator, 'hinf_regressor'):
            estimator.hinf_regressor.solver_params = mosek_solver_params
        else:
            estimator.solver_params = mosek_solver_params
        check(estimator)


class TestExceptions:
    """Test a selection of invalid estimator parameter."""

    X = np.array([
        [1, 2, 3, 4],
        [4, 3, 2, 1],
    ])

    @pytest.mark.parametrize('estimator', [
        lmi_regressors.LmiEdmd(alpha=-1),
        lmi_regressors.LmiEdmd(ratio=-1),
        lmi_regressors.LmiEdmd(ratio=0),
        lmi_regressors.LmiEdmd(reg_method='blah'),
        lmi_regressors.LmiEdmd(inv_method='blah'),
        lmi_regressors.LmiEdmd(picos_eps=-1),
        lmi_regressors.LmiDmdc(alpha=-1),
        lmi_regressors.LmiDmdc(ratio=-1),
        lmi_regressors.LmiDmdc(ratio=0),
        lmi_regressors.LmiDmdc(reg_method='blah'),
        lmi_regressors.LmiDmdc(picos_eps=-1),
        lmi_regressors.LmiEdmdSpectralRadiusConstr(spectral_radius=-1),
        lmi_regressors.LmiEdmdSpectralRadiusConstr(spectral_radius=0),
        lmi_regressors.LmiEdmdSpectralRadiusConstr(max_iter=-1),
        lmi_regressors.LmiEdmdSpectralRadiusConstr(iter_atol=-1),
        lmi_regressors.LmiEdmdSpectralRadiusConstr(iter_rtol=-1),
        lmi_regressors.LmiEdmdSpectralRadiusConstr(alpha=-1),
        lmi_regressors.LmiDmdcSpectralRadiusConstr(spectral_radius=-1),
        lmi_regressors.LmiDmdcSpectralRadiusConstr(spectral_radius=0),
        lmi_regressors.LmiDmdcSpectralRadiusConstr(max_iter=-1),
        lmi_regressors.LmiDmdcSpectralRadiusConstr(iter_atol=-1),
        lmi_regressors.LmiDmdcSpectralRadiusConstr(iter_rtol=-1),
        lmi_regressors.LmiDmdcSpectralRadiusConstr(alpha=-1),
        lmi_regressors.LmiEdmdHinfReg(alpha=-1),
        lmi_regressors.LmiEdmdHinfReg(alpha=0),
        lmi_regressors.LmiEdmdHinfReg(ratio=0),
        lmi_regressors.LmiEdmdHinfReg(weight=(
            'blah',
            np.eye(1),
            np.eye(1),
            np.eye(1),
            np.eye(1),
        )),
        lmi_regressors.LmiEdmdHinfReg(max_iter=-1),
        lmi_regressors.LmiEdmdHinfReg(iter_atol=-1),
        lmi_regressors.LmiEdmdHinfReg(iter_rtol=-1),
        lmi_regressors.LmiDmdcHinfReg(alpha=-1),
        lmi_regressors.LmiDmdcHinfReg(alpha=0),
        lmi_regressors.LmiDmdcHinfReg(ratio=0),
        lmi_regressors.LmiDmdcHinfReg(weight=(
            'blah',
            np.eye(1),
            np.eye(1),
            np.eye(1),
            np.eye(1),
        )),
        lmi_regressors.LmiDmdcHinfReg(max_iter=-1),
        lmi_regressors.LmiDmdcHinfReg(iter_atol=-1),
        lmi_regressors.LmiEdmdDissipativityConstr(alpha=-1),
        lmi_regressors.LmiEdmdDissipativityConstr(alpha=0),
        lmi_regressors.LmiEdmdDissipativityConstr(max_iter=-1),
        lmi_regressors.LmiEdmdDissipativityConstr(iter_atol=-1),
        lmi_regressors.LmiEdmdDissipativityConstr(iter_rtol=-1),
    ])
    def test_invalid_params(self, estimator):
        """Test a selection of invalid estimator parameter."""
        with pytest.raises(ValueError):
            estimator.fit(self.X)
