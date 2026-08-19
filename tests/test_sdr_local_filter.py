"""
The /sdr probe must report only devices physically attached to THIS host — not
networked USRPs (e.g. an X4xx) that UHD discovers over the LAN. Otherwise every
unit on the same subnet would show the X410 under its SDR field.
"""
from agent.system import _parse_uhd_output, _is_local_device, _to_sdr_device


def _local(text):
    return [_to_sdr_device(d) for d in _parse_uhd_output(text) if _is_local_device(d)]


NETWORKED_X410 = """
-- UHD Device 0
Device Address:
    addr: 169.254.1.2
    mgmt_addr: 169.254.1.2
    product: x410
    serial: 329888B
    type: x4xx
"""

X410_ON_ITSELF = """
-- UHD Device 0
Device Address:
    mgmt_addr: 127.0.0.1
    product: x410
    serial: 329888B
    type: x4xx
"""

USB_B200 = """
-- UHD Device 0
Device Address:
    serial: 30ABCDE
    name: MyB206
    product: B200
    type: b200
"""


def test_networked_usrp_is_filtered_out():
    # A Pi with no SDR that merely *discovers* the X410 over the LAN shows nothing.
    assert _local(NETWORKED_X410) == []


def test_onboard_device_is_kept():
    # The X410 querying itself (loopback mgmt_addr, no addr) still shows its radio.
    devs = _local(X410_ON_ITSELF)
    assert len(devs) == 1 and devs[0].product == "x410"


def test_usb_device_is_kept():
    devs = _local(USB_B200)
    assert len(devs) == 1 and devs[0].type == "b200"


def test_mixed_keeps_only_local():
    devs = _local(NETWORKED_X410 + USB_B200)
    assert [d.type for d in devs] == ["b200"]
