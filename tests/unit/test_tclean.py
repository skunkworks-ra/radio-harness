"""
Unit tests for ms_tclean script generation.

Uses real_ms_calibrated (tests/unit/conftest.py) — a real MS with a real
CORRECTED_DATA column — for every test that doesn't care about the
CORRECTED_DATA guardrail itself, so run() takes its real success branch
(column present, no raise, no warning) rather than accidentally falling
through open_table's except-Exception into "Could not verify" on a fake
`table.info`-only MS. TestCorrectedDataGuardrail below covers both real
outcomes of that check directly: present (real_ms_calibrated) and genuinely
absent (real_ms_raw).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _run(tmp_path, real_ms_calibrated, **kwargs):
    from ms_modify.tclean import run

    workdir = tmp_path / "work"
    workdir.mkdir()
    result = run(
        real_ms_calibrated,
        str(workdir / "img"),
        field="0",
        workdir=str(workdir),
        execute=False,
        **kwargs,
    )
    script = Path(result["data"]["script_path"]["value"]).read_text()
    warnings = result.get("warnings", [])
    return script, warnings


class TestCfcache:
    def test_cfcache_rendered_for_awproject(self, tmp_path, real_ms_calibrated):
        script, _ = _run(
            tmp_path,
            real_ms_calibrated,
            gridder="awproject",
            wprojplanes=32,
            cfcache="/data/cf.cache",
        )
        assert "cfcache      = '/data/cf.cache'" in script

    def test_cfcache_ignored_for_awp2_with_warning(self, tmp_path, real_ms_calibrated):
        script, warnings = _run(
            tmp_path, real_ms_calibrated, gridder="awp2", wprojplanes=32, cfcache="/data/cf.cache"
        )
        assert "cfcache      =" not in script
        assert any("only used by gridder='awproject'" in w for w in warnings)

    def test_awproject_without_cfcache_warns(self, tmp_path, real_ms_calibrated):
        _, warnings = _run(tmp_path, real_ms_calibrated, gridder="awproject", wprojplanes=32)
        assert any("without cfcache" in w for w in warnings)


class TestWprojplanesWarning:
    def test_awp2_without_wprojplanes_warns(self, tmp_path, real_ms_calibrated):
        _, warnings = _run(tmp_path, real_ms_calibrated, gridder="awp2")
        assert any("wprojplanes=1" in w for w in warnings)

    def test_standard_gridder_no_warning(self, tmp_path, real_ms_calibrated):
        _, warnings = _run(tmp_path, real_ms_calibrated, gridder="standard")
        assert not any("wprojplanes" in w for w in warnings)


class TestAwpFullPolGuardrails:
    def test_mvc_mtmfs_iquv_warns_shape_assert(self, tmp_path, real_ms_calibrated):
        _, warnings = _run(
            tmp_path,
            real_ms_calibrated,
            gridder="awp2",
            wprojplanes=16,
            stokes="IQUV",
            deconvolver="mtmfs",
            nterms=2,
            specmode="mvc",
        )
        assert any("shapeIn.isEqual" in w for w in warnings)
        assert any("specmode='mfs'" in w for w in warnings)

    def test_mfs_mtmfs_iquv_no_shape_warning(self, tmp_path, real_ms_calibrated):
        # The steered-to combo must NOT emit the crash warning.
        _, warnings = _run(
            tmp_path,
            real_ms_calibrated,
            gridder="awp2",
            wprojplanes=16,
            stokes="IQUV",
            deconvolver="mtmfs",
            nterms=2,
            specmode="mfs",
        )
        assert not any("shapeIn.isEqual" in w for w in warnings)

    def test_awp_fullpol_warns_aterm_cost(self, tmp_path, real_ms_calibrated):
        _, warnings = _run(
            tmp_path,
            real_ms_calibrated,
            gridder="awp2",
            wprojplanes=16,
            stokes="IQUV",
            specmode="mfs",
        )
        assert any("A-term" in w for w in warnings)

    def test_stokes_i_no_fullpol_warnings(self, tmp_path, real_ms_calibrated):
        _, warnings = _run(tmp_path, real_ms_calibrated, gridder="awp2", wprojplanes=16, stokes="I")
        assert not any("A-term" in w for w in warnings)
        assert not any("shapeIn.isEqual" in w for w in warnings)

    def test_standard_gridder_no_fullpol_warnings(self, tmp_path, real_ms_calibrated):
        _, warnings = _run(tmp_path, real_ms_calibrated, gridder="standard", stokes="IQUV")
        assert not any("A-term" in w for w in warnings)


class TestSpecmodeMvc:
    def test_mvc_rendered(self, tmp_path, real_ms_calibrated):
        script, _ = _run(
            tmp_path, real_ms_calibrated, gridder="awp2", wprojplanes=32, specmode="mvc"
        )
        assert "specmode     = 'mvc'" in script


class TestCubeArgs:
    def test_cube_args_rendered_for_cube(self, tmp_path, real_ms_calibrated):
        script, _ = _run(
            tmp_path,
            real_ms_calibrated,
            specmode="cube",
            stokes="IQUV",
            nchan=16,
            start="1.0GHz",
            width="64MHz",
            outframe="LSRK",
        )
        assert "specmode     = 'cube'" in script
        assert "nchan        = 16" in script
        assert "start        = '1.0GHz'" in script
        assert "width        = '64MHz'" in script
        assert "outframe     = 'LSRK'" in script

    def test_cube_args_ignored_for_mfs_with_warning(self, tmp_path, real_ms_calibrated):
        script, warnings = _run(
            tmp_path, real_ms_calibrated, specmode="mfs", nchan=16, outframe="LSRK"
        )
        assert "nchan" not in script
        assert "outframe" not in script
        assert any("cube args" in w for w in warnings)

    def test_no_cube_args_no_warning(self, tmp_path, real_ms_calibrated):
        script, warnings = _run(tmp_path, real_ms_calibrated, specmode="mfs")
        assert not any("cube args" in w for w in warnings)
        assert "nchan" not in script

    def test_partial_cube_args_omits_unset(self, tmp_path, real_ms_calibrated):
        script, _ = _run(tmp_path, real_ms_calibrated, specmode="cube", nchan=8)
        assert "nchan        = 8" in script
        assert "start" not in script
        assert "width" not in script
        assert "outframe" not in script


class TestConvergence:
    def test_script_requests_compact_summary_and_checks_convergence(
        self, tmp_path, real_ms_calibrated
    ):
        script, _ = _run(tmp_path, real_ms_calibrated)
        assert "fullsummary  = False" in script
        assert "summary = tclean(" in script
        assert 'summary.get("stopcode")' in script
        assert "DID NOT CONVERGE" in script

    def test_convergence_classifier(self):
        from ms_modify.tclean import _convergence

        code, _desc, converged, warn = _convergence({"stopcode": 2})
        assert code == 2 and converged and warn is None

        code, _desc, converged, warn = _convergence({"stopcode": 1})
        assert code == 1 and not converged and "did NOT converge" in warn

        code, _desc, converged, warn = _convergence(None)
        assert code is None and not converged and "no summary dict" in warn


class TestCorrectedDataGuardrail:
    """Both real outcomes of the CORRECTED_DATA check, against real MSs."""

    def test_raises_when_corrected_data_is_genuinely_absent(self, tmp_path, real_ms_raw):
        from ms_inspect.exceptions import InsufficientMetadataError
        from ms_modify.tclean import run

        workdir = tmp_path / "work"
        workdir.mkdir()
        with pytest.raises(InsufficientMetadataError, match="CORRECTED_DATA column not found"):
            run(
                real_ms_raw,
                str(workdir / "img"),
                field="0",
                workdir=str(workdir),
                execute=False,
            )

    def test_no_warning_when_corrected_data_is_genuinely_present(
        self, tmp_path, real_ms_calibrated
    ):
        _, warnings = _run(tmp_path, real_ms_calibrated)
        assert not any("Could not verify" in w for w in warnings)


class TestScales:
    def test_scales_rendered_for_multiscale(self, tmp_path, real_ms_calibrated):
        script, warnings = _run(
            tmp_path, real_ms_calibrated, deconvolver="multiscale", scales=[0, 4, 12]
        )
        assert "scales       = [0, 4, 12]" in script
        assert not any("ignores scales" in w or "hogbom" in w for w in warnings)

    def test_scales_rendered_for_mtmfs(self, tmp_path, real_ms_calibrated):
        script, warnings = _run(
            tmp_path, real_ms_calibrated, deconvolver="mtmfs", nterms=2, scales=[0, 6]
        )
        assert "scales       = [0, 6]" in script
        assert not any("ignores scales" in w or "hogbom" in w for w in warnings)

    def test_scales_with_hogbom_passes_through_and_warns(self, tmp_path, real_ms_calibrated):
        # CASA ignores it; the warning is so the model stops refining scales it
        # has decided not to use.
        script, warnings = _run(tmp_path, real_ms_calibrated, deconvolver="hogbom", scales=[0, 4])
        assert "scales       = [0, 4]" in script
        assert any("ignores scales" in w for w in warnings)

    def test_multiscale_without_scales_warns_it_is_hogbom(self, tmp_path, real_ms_calibrated):
        script, warnings = _run(tmp_path, real_ms_calibrated, deconvolver="multiscale")
        assert "scales       =" not in script
        assert any("defaults to [0], which is hogbom" in w for w in warnings)

    def test_no_scales_no_warning(self, tmp_path, real_ms_calibrated):
        script, warnings = _run(tmp_path, real_ms_calibrated)
        assert "scales       =" not in script
        assert not any("ignores scales" in w or "hogbom" in w for w in warnings)


class TestFieldOfView:
    """The image must hold every selected pointing's first PB sidelobe. The
    tool measures the requirement from the MS and warns; it never resizes."""

    def _fov(self, tmp_path, real_ms_calibrated, **kwargs):
        from ms_modify.tclean import run

        workdir = tmp_path / "work"
        workdir.mkdir(exist_ok=True)
        result = run(
            real_ms_calibrated, str(workdir / "img"), field="0", workdir=str(workdir), **kwargs
        )
        return result["data"]["field_of_view"], result.get("warnings", [])

    def test_measurement_is_reported_for_citation(self, tmp_path, real_ms_calibrated):
        fov, _ = self._fov(tmp_path, real_ms_calibrated)
        v = fov["value"]
        assert fov["flag"] == "COMPLETE"
        assert v["n_fields"] == 1 and v["mosaic_extent_arcsec"] == 0.0
        assert v["pb_fwhm_arcsec"] > 0 and v["dish_diameter_m"] > 0
        assert v["required_arcsec"] == pytest.approx(3 * v["pb_fwhm_arcsec"], rel=1e-6)

    def test_short_image_warns_with_numbers_and_keeps_imsize(self, tmp_path, real_ms_calibrated):
        fov, warnings = self._fov(tmp_path, real_ms_calibrated, cell="1arcsec", imsize=[16, 16])
        w = [x for x in warnings if "will alias" in x]
        assert len(w) == 1
        assert "lowest selected frequency" in w[0]
        assert f"needs {fov['value']['required_arcsec']:.0f} arcsec" in w[0]
        assert "imsize as given" in w[0]

    def test_adequate_image_does_not_warn(self, tmp_path, real_ms_calibrated):
        fov, _ = self._fov(tmp_path, real_ms_calibrated)
        need = fov["value"]["required_arcsec"]
        cell = 10.0
        n = int(-(-need // cell)) + 8
        _, warnings = self._fov(tmp_path, real_ms_calibrated, cell=f"{cell}arcsec", imsize=[n, n])
        assert not any("will alias" in x for x in warnings)

    def test_uses_lowest_frequency_of_selected_spws(self):
        # Widest beam sets the extent: PB FWHM scales as 1/freq.
        from ms_modify.tclean import _ARCSEC_PER_RAD, _C_M_S

        pb_at = lambda f_hz, d: 1.02 * (_C_M_S / f_hz) / d * _ARCSEC_PER_RAD  # noqa: E731
        assert pb_at(1.0e9, 25.0) == pytest.approx(2 * pb_at(2.0e9, 25.0))

    def test_composite_rounding(self):
        from ms_modify.tclean import _next_composite

        assert _next_composite(512) == 512
        assert _next_composite(561) == 576
        assert _next_composite(1025) == 1080

    def test_unparseable_cell_skips_the_check(self, tmp_path, real_ms_calibrated):
        from ms_modify.tclean import _cell_arcsec

        assert _cell_arcsec("4arcsec") == 4.0
        assert _cell_arcsec("0.5arcmin") == 30.0
        assert _cell_arcsec("bogus") is None
        _, warnings = self._fov(tmp_path, real_ms_calibrated, cell="bogus", imsize=[8, 8])
        assert not any("will alias" in x for x in warnings)
