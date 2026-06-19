from typing import Dict, List, Tuple, Optional
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
        
        fields = self.regs_fields[name]
        sorted_fields = sorted(fields, key=lambda x: x[0])
        total_bits = sum(width for _, _, width in fields)
        max_value = (1 << total_bits) - 1

        if not (0 <= value <= max_value):
            raise ValueError(f"Value {value} out of range for {name} (0..{max_value})")

        remaining = value
        for addr, shift, width in sorted_fields:
            part = remaining & ((1 << width) - 1)
            current = self.client.read_byte(addr)
            mask = ((1 << width) - 1) << shift
            new_val = (current & ~mask) | ((part << shift) & mask)
            if new_val != current:
                self.client.write_byte(new_val, addr)
            remaining >>= width
        return True
    
    def set_amux(self, signal_name: str) -> bool:
        if signal_name not in self.amux_signals:
            raise KeyError(f"Unknown signal name: {signal_name}")

        signal_num = self.amux_signals[signal_name]
        value = 1 << signal_num

        return self.set_data("TEST_MUX", value)
    
    def set_default(self):
        for addr, value in self.default_registers.items():
            self.client.write_byte(value, addr)

        print(f"Default registers loaded: {len(self.default_registers)} registers written.")
    