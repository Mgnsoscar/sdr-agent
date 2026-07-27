NAME = "/dev/ttyACM0"


class Port(object):
    _name: str

    def __init__(self, name: str) -> None:
        self._name = name

    def _write(self, message: str) -> None:
        with open(self._name, "w") as port:
            port.write(message)

    def _read(self) -> str:
        with open(self._name, "r") as port:
            return port.readline()

class Attenuator(Port):
    _channel: int

    def __init__(self, name: str, channel: int) -> None:
        super().__init__(name)
        self._channel = channel

    def set_attenuation(self, attenuation: float) -> None:
        self._write(f"SET {self._channel} = {attenuation}")
        print(self._read())

if __name__ == "__main__":

    import time
    attenuator = Attenuator(NAME, 1)

    for i in range(90, 0, 10):
        attenuator.set_attenuation(i)
        time.sleep(1)