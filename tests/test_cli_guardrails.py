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
