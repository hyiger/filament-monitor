"""Config precedence and validation tests: CLI > TOML > built-in defaults.

Regression tests for the two-pass config parse (issue #23) and the
post-merge validation step (issue #34). All tests go through the real
parse/merge/validate stage (filmon.cli.parse_config).
"""

import json

import pytest

from filmon.cli import parse_config
from filmon.doctor import build_arg_parser, resolved_config_dict


def _write_toml(tmp_path, text):
    cfg = tmp_path / "config.toml"
    cfg.write_text(text, encoding="utf-8")
    return str(cfg)


def test_toml_only_value_applies(tmp_path):
    """A value present only in the TOML file must reach the parsed args."""
    path = _write_toml(tmp_path, "[detection]\njam_timeout = 4.5\n")
    args = parse_config(["--config", path])
    assert args.jam_timeout == 4.5


def test_cli_overrides_toml(tmp_path):
    path = _write_toml(tmp_path, "[detection]\njam_timeout = 4.5\n")
    args = parse_config(["--config", path, "--jam-timeout", "9.0"])
    assert args.jam_timeout == 9.0


def test_builtin_default_when_neither():
    args = parse_config(["-p", "/dev/ttyACM0"])
    assert args.jam_timeout == 8.0


def test_toml_value_per_section_survives(tmp_path):
    """One key per TOML section must survive the merge (regression for issue #23)."""
    path = _write_toml(
        tmp_path,
        "\n".join(
            [
                "[serial]",
                'port = "/dev/ttyUSB1"',
                "baud = 250000",
                "[gpio]",
                "motion_gpio = 5",
                "runout_enabled = true",
                "rearm_button_active_high = true",
                "[detection]",
                "jam_timeout = 4.0",
                "arm_grace_pulses = 12",
                "arm_grace_s = 12.0",
                "[logging]",
                "breadcrumb_interval = 7.5",
                "[control]",
                'socket = "/tmp/filmon-test.sock"',
            ]
        )
        + "\n",
    )
    args = parse_config(["--config", path])
    assert args.port == "/dev/ttyUSB1"
    assert args.baud == 250000
    assert args.motion_gpio == 5
    assert args.runout_enabled is True
    assert args.rearm_button_active_high is True
    assert args.jam_timeout == 4.0
    assert args.arm_grace_pulses == 12
    assert args.arm_grace_s == 12.0
    assert args.breadcrumb_interval == 7.5
    assert args.control_socket == "/tmp/filmon-test.sock"


def test_no_control_socket_wins_over_toml(tmp_path):
    """An explicit CLI disable must not be clobbered by the TOML socket path."""
    path = _write_toml(tmp_path, '[control]\nsocket = "/tmp/filmon-test.sock"\n')
    args = parse_config(["--config", path, "--no-control-socket"])
    assert args.control_socket == ""


def test_jam_timeout_adaptive_toml_reaches_args_and_print_config(tmp_path):
    """jam_timeout_adaptive must flow through args, not a raw-TOML side channel."""
    path = _write_toml(tmp_path, "[detection]\njam_timeout_adaptive = true\n")
    args = parse_config(["--config", path])
    assert args.jam_timeout_adaptive is True
    # --print-config must agree with what the monitor would receive.
    resolved = json.loads(json.dumps(resolved_config_dict(args)))
    assert resolved["detection"]["jam_timeout_adaptive"] is True


def test_no_jam_timeout_adaptive_overrides_toml(tmp_path):
    path = _write_toml(tmp_path, "[detection]\njam_timeout_adaptive = true\n")
    args = parse_config(["--config", path, "--no-jam-timeout-adaptive"])
    assert args.jam_timeout_adaptive is False


def test_new_flags_parse():
    args = parse_config(
        [
            "-p", "/dev/ttyACM0",
            "--jam-timeout-adaptive",
            "--jam-timeout-min", "2.0",
            "--jam-timeout-max", "20.0",
            "--jam-timeout-k", "12.0",
            "--jam-timeout-pps-floor", "0.5",
            "--jam-timeout-ema-halflife", "4.0",
            "--arm-grace-pulses", "6",
            "--arm-grace-s", "5.5",
        ]
    )
    assert args.jam_timeout_adaptive is True
    assert args.jam_timeout_min == 2.0
    assert args.jam_timeout_max == 20.0
    assert args.jam_timeout_k == 12.0
    assert args.jam_timeout_pps_floor == 0.5
    assert args.jam_timeout_ema_halflife == 4.0
    assert args.arm_grace_pulses == 6
    assert args.arm_grace_s == 5.5


def test_validation_rejects_min_greater_than_max():
    with pytest.raises(SystemExit):
        parse_config(["--jam-timeout-min", "10.0", "--jam-timeout-max", "5.0"])


def test_validation_rejects_nonpositive_jam_timeout():
    with pytest.raises(SystemExit):
        parse_config(["--jam-timeout", "-1.0"])
    with pytest.raises(SystemExit):
        parse_config(["--jam-timeout", "0"])


def test_validation_rejects_negative_debounce_and_grace():
    with pytest.raises(SystemExit):
        parse_config(["--rearm-button-debounce", "-0.1"])
    with pytest.raises(SystemExit):
        parse_config(["--arm-grace-s", "-1.0"])
    with pytest.raises(SystemExit):
        parse_config(["--breadcrumb-interval", "-2.0"])


def test_validation_rejects_non_numeric_toml_baud(tmp_path):
    path = _write_toml(tmp_path, '[serial]\nbaud = "fast"\n')
    with pytest.raises(SystemExit):
        parse_config(["--config", path])


def test_validation_rejects_boolean_toml_gpio(tmp_path):
    path = _write_toml(tmp_path, "[gpio]\nmotion_gpio = true\n")
    with pytest.raises(SystemExit):
        parse_config(["--config", path])


def test_numeric_toml_values_coerced(tmp_path):
    """TOML integers for float keys (and numeric strings) are coerced, not rejected."""
    path = _write_toml(tmp_path, '[detection]\njam_timeout = 5\n\n[serial]\nbaud = "250000"\n')
    args = parse_config(["--config", path])
    assert isinstance(args.jam_timeout, float)
    assert args.jam_timeout == 5.0
    assert isinstance(args.baud, int)
    assert args.baud == 250000


def test_malformed_stall_thresholds_fails_at_startup():
    with pytest.raises(SystemExit):
        parse_config(["--stall-thresholds", "3,abc"])


def test_print_config_includes_rearm_button_settings():
    args = parse_config(["-p", "/dev/ttyACM0"])
    gpio = resolved_config_dict(args)["gpio"]
    assert gpio["rearm_button_gpio"] is None
    assert gpio["rearm_button_debounce"] == 0.25
    assert gpio["rearm_button_long_press"] == 1.5
    assert gpio["rearm_button_active_high"] is False


# ---------------- Codex review follow-ups ----------------

def test_non_finite_timeouts_rejected(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[detection]\njam_timeout = nan\n")
    with pytest.raises(SystemExit, match="finite"):
        parse_config(["--config", str(cfg)])
    cfg.write_text("[detection]\njam_timeout_min = inf\n")
    with pytest.raises(SystemExit, match="finite"):
        parse_config(["--config", str(cfg)])


def test_fractional_integer_fields_rejected(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[gpio]\nmotion_gpio = 26.9\n")
    with pytest.raises(SystemExit, match="integer"):
        parse_config(["--config", str(cfg)])


def test_string_boolean_rejected(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('[gpio]\nrunout_enabled = "false"\n')
    with pytest.raises(SystemExit, match="true or false"):
        parse_config(["--config", str(cfg)])


def test_nonpositive_adaptive_bounds_rejected(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[detection]\njam_timeout_min = 0\njam_timeout_max = 0\n")
    with pytest.raises(SystemExit, match="must be > 0"):
        parse_config(["--config", str(cfg)])
    cfg.write_text("[detection]\njam_timeout_k = -1\n")
    with pytest.raises(SystemExit, match="jam_timeout_k"):
        parse_config(["--config", str(cfg)])


def test_no_control_socket_works_on_bare_parser():
    # Direct build_arg_parser() consumers (re-exported for wrappers/tests) must
    # keep the old behavior: the flag itself disables the socket.
    ap = build_arg_parser()
    args = ap.parse_args(["--no-control-socket"])
    assert args.control_socket == ""


def test_runout_active_low_overrides_toml(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[gpio]\nrunout_enabled = true\nrunout_active_high = true\n")
    args = parse_config(["--config", str(cfg), "--runout-active-low"])
    assert args.runout_active_high is False
