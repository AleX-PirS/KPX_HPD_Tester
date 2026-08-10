from mgpd import MGPDClient


class Configuration:
    def __init__(
        self,
        client: MGPDClient,
        DEFAULT_REGISTERS: dict[int, int],
        AMUX_SIGNALS: dict[str, int],
        REGS_FIELDS: dict[str, list[tuple[int, int, int]]],
        AMUX_MAP: dict[int, tuple[int, int]],
    ):
        self.client = client
        self.default_registers = DEFAULT_REGISTERS
        self.amux_signals = AMUX_SIGNALS
        self.regs_fields = REGS_FIELDS
        self.amux_map = AMUX_MAP

    def set_data(self, name: str, value: int) -> bool:
        if name not in self.regs_fields:
            raise KeyError(f"Unknown register field: {name}")

        # Порядок фрагментов в REGS_FIELDS является значимым:
        # первый элемент списка содержит младшие биты значения,
        # следующий элемент - более старшие биты.
        fields = self.regs_fields[name]
        total_bits = sum(width for _, _, width in fields)
        max_value = (1 << total_bits) - 1

        if not (0 <= value <= max_value):
            raise ValueError(
                f"Value {value} out of range for {name} (0..{max_value})"
            )

        remaining = value

        for addr, shift, width in fields:
            part = remaining & ((1 << width) - 1)
            current = self.client.read_byte(addr)

            if current is None:
                return False

            mask = ((1 << width) - 1) << shift
            new_val = (current & ~mask) | ((part << shift) & mask)

            if new_val != current:
                if not self.client.write_byte(new_val, addr):
                    return False

            remaining >>= width

        return True

    def set_amux(self, signal_name: str) -> bool:
        if signal_name not in self.amux_signals:
            raise KeyError(f"Unknown signal name: {signal_name}")

        signal_num = self.amux_signals[signal_name]
        value = 1 << signal_num

        return self.set_data("TEST_MUX", value)

    def set_default(self) -> bool:
        for addr, value in self.default_registers.items():
            if not self.client.write_byte(value, addr):
                return False

        print(
            f"Default registers loaded: "
            f"{len(self.default_registers)} registers written."
        )
        return True


    def read_registers(self, addresses=None) -> dict[int, int]:
        """Read physical register bytes once and return {address: value}.

        Existing write API is unchanged. This helper is primarily useful for GUI
        synchronization and for future automated diagnostics.
        """
        if addresses is None:
            addresses = sorted({
                addr
                for fragments in self.regs_fields.values()
                for addr, _, _ in fragments
            })
        else:
            addresses = sorted(set(addresses))

        result = {}
        for addr in addresses:
            value = self.client.read_byte(addr)
            if value is None:
                raise RuntimeError(f"Failed to read register 0x{addr:04X}")
            result[addr] = value
        return result

    def get_data(self, name: str, register_cache: dict[int, int] | None = None) -> int:
        """Read and decode one logical field using REGS_FIELDS.

        Fragment order is the same as in set_data(): first fragment contains the
        least-significant bits of the logical value.
        """
        if name not in self.regs_fields:
            raise KeyError(f"Unknown register field: {name}")

        fields = self.regs_fields[name]
        if register_cache is None:
            register_cache = self.read_registers(addr for addr, _, _ in fields)

        value = 0
        value_shift = 0
        for addr, shift, width in fields:
            if addr not in register_cache:
                byte = self.client.read_byte(addr)
                if byte is None:
                    raise RuntimeError(f"Failed to read register 0x{addr:04X}")
                register_cache[addr] = byte

            part = (register_cache[addr] >> shift) & ((1 << width) - 1)
            value |= part << value_shift
            value_shift += width

        return value

    def get_all_data(self, register_cache: dict[int, int] | None = None) -> dict[str, int]:
        """Read all physical registers once and decode all logical fields."""
        if register_cache is None:
            register_cache = self.read_registers()

        return {
            name: self.get_data(name, register_cache=register_cache)
            for name in self.regs_fields
        }

    def get_amux(self, register_cache: dict[int, int] | None = None) -> str | None:
        """Return active AMUX signal name for a valid one-hot TEST_MUX value."""
        value = self.get_data("TEST_MUX", register_cache=register_cache)
        if value <= 0 or value & (value - 1):
            return None

        bit = value.bit_length() - 1
        for name, number in self.amux_signals.items():
            if number == bit:
                return name
        return None

    def set_ctrl(self, state: int | bool) -> bool:
        """Установить CTRL в статическое состояние 0 или 1."""
        return self.client.set_ctrl(state)

    def set_ctrl_pwm(self, frequency_khz: int, width_ns: int) -> bool:
        """Установить CTRL в ШИМ-режим."""
        return self.client.set_ctrl_pwm(frequency_khz, width_ns)