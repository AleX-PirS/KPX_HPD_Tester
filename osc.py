import pyvisa as visa


class OscilloscopeDCLevelLogger:
    def __init__(
        self,
        osc_address: str | None = None,
        idn_substring: str = "DSO",
        avg_count: int = 16,
        timeout_ms: int = 5000,
        chunk_size: int = 1_000_000,
    ):
        self.osc_address = osc_address
        self.idn_substring = idn_substring
        self.avg_count = avg_count

        self.rm = visa.ResourceManager("@py")
        self.osc = self._connect_oscilloscope()

        self.osc.timeout = timeout_ms
        self.osc.chunk_size = chunk_size
        self.osc.read_termination = "\n"
        self.osc.write_termination = "\n"

        self._configure_oscilloscope()

    # ---------------- low-level VISA ----------------

    def _write(self, command: str):
        self.osc.write(command)

    def _query(self, command: str) -> str:
        return self.osc.query(command).strip()

    # ---------------- connection ----------------

    def _connect_oscilloscope(self):
        if self.osc_address is not None:
            osc = self.rm.open_resource(self.osc_address)
            idn = osc.query("*IDN?").strip()

            if self.idn_substring not in idn:
                osc.close()
                raise RuntimeError(
                    f"Wrong instrument. Expected '{self.idn_substring}', got '{idn}'."
                )

            print(f"Connected: {idn}")
            return osc

        resources = self.rm.list_resources("TCPIP?*")

        for resource_name in resources:
            try:
                osc = self.rm.open_resource(resource_name)
                osc.timeout = 3000
                osc.read_termination = "\n"
                osc.write_termination = "\n"

                idn = osc.query("*IDN?").strip()

                if self.idn_substring in idn:
                    print(f"Connected: {resource_name} -> {idn}")
                    return osc

                osc.close()

            except Exception:
                pass

        raise RuntimeError(
            f"Oscilloscope with IDN substring '{self.idn_substring}' not found."
        )

    # ---------------- oscilloscope setup ----------------

    def _configure_oscilloscope(self):
        """
        No reset.
        No trigger configuration.
        No generator configuration.
        """

        # Use only channel 1
        self._write(":CHAN1 ON")
        self._write(":CHAN1:INP DC")

        # Analog averaging mode
        self._write(f":ACQ:AVER:COUN {self.avg_count}")
        self._write(":ACQ:AVER ON")

        # Waveform source
        self._write(":WAV:SOUR CHAN1")
        self._write(":WAV:FORM ASC")
        self._write(":ACQ:POIN:AUTO ON")

        # Config timescale
        self._write(":TIM:SCAL {50e9}")

        # Continuous acquisition
        self._write(":RUN")

    # ---------------- measurement ----------------

    @staticmethod
    def _strip_scpi_block_header(data: str) -> str:
        data = data.strip()

        if not data.startswith("#"):
            return data

        digits_count = int(data[1])
        length_start = 2
        length_end = length_start + digits_count

        data_length = int(data[length_start:length_end])
        payload_start = length_end
        payload_end = payload_start + data_length

        return data[payload_start:payload_end]

    def read_level(self) -> float:
        """
        Reads one DC level from CH1.
        The level is calculated as mean value of the averaged waveform.
        """

        self._write(":WAV:SOUR CHAN1")
        raw = self._query(":WAV:DATA?")
        raw = self._strip_scpi_block_header(raw)

        values = [
            float(item)
            for item in raw.split(",")
            if item.strip() != ""
        ]

        if len(values) == 0:
            raise RuntimeError("Empty waveform data.")

        return sum(values) / len(values)

    def save(self) -> float:
        """
        Top method.
        Call this method once per scan point.
        It saves exactly one number: averaged DC level from CH1.
        """

        level_v = self.read_level()

        print(f"Saved: CH1={level_v:.9g} V")
        return level_v

    def close(self):
        self.osc.close()
        self.rm.close()