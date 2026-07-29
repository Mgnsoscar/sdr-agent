import argparse


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



def main():

    attenuator = Attenuator(NAME, 1)

    p = argparse.ArgumentParser(description="Attenuator")

    p.add_argument("-Attenuation", "--start", type=float, required=True, help="New attenuation value [dB].")

    opt = p.parse_args()

    try:
        attenuator.set_attenuation(opt.Attenuation)
    except Exception as e:
        print("Failed to set attenuation to " + str(opt.Attenuation))


if __name__ == "__main__":

    main()