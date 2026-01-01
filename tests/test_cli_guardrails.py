def test_parser_defaults():
    m = load_module()
    ap = m.build_arg_parser()
    args = ap.parse_args(["-p", "/dev/ttyACM0"])
    assert args.port == "/dev/ttyACM0"
    assert args.runout_enabled is False
    assert args.runout_gpio == 27
    assert args.runout_debounce is None
    assert args.runout_active_high is False

def test_runout_guardrails_ignore_when_disabled():
    m = load_module()
    ap = m.build_arg_parser()
    args = ap.parse_args([
        "-p", "/dev/ttyACM0",
        "--runout-gpio", "27",
        "--runout-debounce", "0.2",
        "--runout-active-high",
    ])
    ignored = m.apply_runout_guardrails(args)
    assert ignored == ["--runout-active-high", "--runout-debounce", "--runout-gpio"]
    # The settings should be neutralized
    assert args.runout_gpio is None
    assert args.runout_debounce is None
    assert args.runout_active_high is False

def test_runout_guardrails_respected_when_enabled():
    m = load_module()
    ap = m.build_arg_parser()
    args = ap.parse_args([
        "-p", "/dev/ttyACM0",
        "--runout-enabled",
        "--runout-gpio", "22",
        "--runout-debounce", "0.15",
        "--runout-active-high",
    ])
    ignored = m.apply_runout_guardrails(args)
    assert ignored == []
    assert args.runout_gpio == 22
    assert args.runout_debounce == 0.15
    assert args.runout_active_high is True


def test_config_provides_port_without_cli_port(tmp_path):
    """Regression: --config should satisfy the normal-mode port requirement."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "filament-monitor.py"

    cfg = tmp_path / "config.toml"
    cfg.write_text('[serial]\nport = "/dev/ttyACM0"\nbaud = 115200\n', encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(script), "--config", str(cfg), "--print-config"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["serial"]["port"] == "/dev/ttyACM0"
