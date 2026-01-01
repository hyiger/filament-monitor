def test_control_markers_enable_arm_unarm_disable_reset():
    m = load_module()

    class CapturingLogger(m.JsonLogger):
        def __init__(self):
            super().__init__(enable_json=False)
            self.events = []
        def emit(self, event: str, **fields):
            self.events.append((event, fields))

    logger = CapturingLogger()
    state = m.MonitorState()
    mon = m.FilamentMonitor(
        state=state,
        logger=logger,
        motion_gpio=26,
        runout_gpio=27,
        runout_active_high=False,
        runout_debounce_s=0.0,
        jam_timeout_s=8.0,
        arm_min_pulses=12,
        pause_gcode="M600",
        verbose=False,
    )

    mon._handle_control_marker("M118 A1 filmon:enable")
    assert mon.state.enabled is True
    assert mon.state.armed is False
    assert logger.events[-1][0] == "enabled"

    mon._handle_control_marker("M118 A1 filmon:arm")
    assert mon.state.enabled is True
    assert mon.state.armed is True
    assert logger.events[-1][0] == "armed"

    mon._handle_control_marker("M118 A1 filmon:unarm")
    assert mon.state.enabled is True
    assert mon.state.armed is False
    assert logger.events[-1][0] == "unarmed"

    mon._handle_control_marker("M118 A1 filmon:disable")
    assert mon.state.enabled is False
    assert mon.state.armed is False
    assert logger.events[-1][0] == "disabled"

    mon._handle_control_marker("M118 A1 filmon:reset")
    assert mon.state.enabled is False
    assert mon.state.latched is False
    assert mon.state.armed is False
    assert logger.events[-1][0] == "reset"
