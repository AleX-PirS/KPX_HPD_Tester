import logging
import socket

logger = logging.getLogger(__name__)


class MGPDClient:
    """
    Синхронный TCP-клиент для управления микросхемой через MGPDLab.

    Поддерживаемые команды:
        READ_BYTE FROM 0xAAAA
        WRITE_BYTE 0xDD TO 0xAAAA
        SET_CTRL_PIN 0|1
        SET_CTRL_PIN F=<kHz> W=<ns>

    При подключении может автоматически активировать KIPIX CONTROL
    записью 0xA5 по адресу 0x803C.
    """

    KIPIX_CONTROL_ADDRESS = 0x803C
    KIPIX_CONTROL_ENABLE_VALUE = 0xA5

    CTRL_PWM_MIN_FREQUENCY_KHZ = 100
    CTRL_PWM_MAX_FREQUENCY_KHZ = 50_000
    CTRL_PWM_FREQUENCY_STEP_KHZ = 10
    CTRL_PWM_WIDTH_STEP_NS = 10

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0xBEEB,
        timeout: float = 5.0,
        auto_enable_kipix: bool = True,
        trace_callback=None,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.auto_enable_kipix = auto_enable_kipix
        self.trace_callback = trace_callback
        self._socket: socket.socket | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self):
        """Установить TCP-соединение с MGPDLab."""
        if self._connected:
            self.disconnect()

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(self.timeout)

        try:
            self._socket.connect((self.host, self.port))
            self._connected = True
            logger.info("Connected to %s:%s", self.host, self.port)

            if self.auto_enable_kipix:
                if not self.enable_kipix_control():
                    raise RuntimeError(
                        "Failed to enable KIPIX CONTROL "
                        f"(write 0x{self.KIPIX_CONTROL_ENABLE_VALUE:02X} "
                        f"to 0x{self.KIPIX_CONTROL_ADDRESS:04X})"
                    )
                logger.info("KIPIX CONTROL enabled")

        except Exception as error:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
            self._connected = False
            raise ConnectionError(
                f"Failed to connect or enable KIPIX: {error}"
            ) from error

        return self

    def disconnect(self):
        """Закрыть TCP-соединение."""
        if self._socket is not None:
            self._socket.close()
            self._socket = None

        self._connected = False
        logger.info("Disconnected")

    def _send_command(self, cmd: bytes) -> bytes:
        """Отправить одну команду и вернуть ответ MGPDLab."""
        if not self._connected or self._socket is None:
            raise RuntimeError("Not connected. Call connect() first.")

        if self.trace_callback is not None:
            self.trace_callback("TX", cmd.decode("ascii", errors="replace").strip())

        self._socket.sendall(cmd)
        response = self._socket.recv(1024)

        if self.trace_callback is not None:
            self.trace_callback("RX", self._decode_response(response))

        return response

    @staticmethod
    def _decode_response(response: bytes) -> str:
        """Декодировать текстовый ответ сервера."""
        for encoding in ("utf-8", "cp1251", "ascii"):
            try:
                return response.decode(encoding).strip()
            except UnicodeDecodeError:
                continue

        return response.decode("latin-1", errors="replace").strip()

    @classmethod
    def _check_error(cls, response: bytes) -> bool:
        """True, если сервер вернул ERROR или ОШИБКА."""
        text = cls._decode_response(response).upper()
        return "ERROR" in text or "ОШИБКА" in text

    @classmethod
    def _check_ok(cls, response: bytes) -> bool:
        """True, если ответ содержит OK и не содержит ошибку."""
        if cls._check_error(response):
            return False
        return "OK" in cls._decode_response(response).upper()

    @staticmethod
    def _validate_address(address: int):
        if not isinstance(address, int) or isinstance(address, bool):
            raise TypeError("address must be int")
        if not 0 <= address <= 0xFFFF:
            raise ValueError("address must be in range 0x0000..0xFFFF")

    @staticmethod
    def _validate_byte(value: int):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError("value must be int")
        if not 0 <= value <= 0xFF:
            raise ValueError("value must be in range 0x00..0xFF")

    def enable_kipix_control(self) -> bool:
        """Активировать KIPIX CONTROL."""
        return self.write_byte(
            self.KIPIX_CONTROL_ENABLE_VALUE,
            self.KIPIX_CONTROL_ADDRESS,
        )

    def write_byte(self, value: int, address: int) -> bool:
        """Записать один байт по 16-битному адресу."""
        self._validate_byte(value)
        self._validate_address(address)

        cmd = f"WRITE_BYTE 0x{value:02X} TO 0x{address:04X}\r\n".encode("ascii")
        response = self._send_command(cmd)

        if self._check_error(response):
            logger.error(
                "Write error: addr=0x%04X, val=0x%02X, response=%r",
                address,
                value,
                response,
            )
            return False

        if not self._check_ok(response):
            logger.error(
                "Unexpected WRITE_BYTE response: addr=0x%04X, val=0x%02X, response=%r",
                address,
                value,
                response,
            )
            return False

        logger.debug("Write OK: addr=0x%04X, val=0x%02X", address, value)
        return True

    def read_byte(self, address: int) -> int | None:
        """Прочитать один байт по 16-битному адресу."""
        self._validate_address(address)

        cmd = f"READ_BYTE FROM 0x{address:04X}\r\n".encode("ascii")
        response = self._send_command(cmd)

        if self._check_error(response):
            logger.error(
                "Read error: addr=0x%04X, response=%r",
                address,
                response,
            )
            return None

        response_text = self._decode_response(response)

        try:
            value = int(response_text, 16)
        except ValueError:
            logger.error("Failed to parse READ_BYTE response: %r", response_text)
            return None

        if not 0 <= value <= 0xFF:
            logger.error("READ_BYTE returned value outside byte range: %r", response_text)
            return None

        return value

    def set_ctrl(self, state: int | bool) -> bool:
        """
        Установить CTRL в статическое состояние 0 или 1.

        Пример команды MGPDLab:
            SET_CTRL_PIN 1
        """
        if isinstance(state, bool):
            state = int(state)

        if not isinstance(state, int) or state not in (0, 1):
            raise ValueError("CTRL state must be 0 or 1")

        cmd = f"SET_CTRL_PIN {state}".encode("ascii")
        response = self._send_command(cmd)

        if not self._check_ok(response):
            logger.error(
                "SET_CTRL_PIN error: state=%d, response=%r",
                state,
                response,
            )
            return False

        logger.debug("CTRL=%d", state)
        return True

    def set_ctrl_pwm(self, frequency_khz: int, width_ns: int) -> bool:
        """
        Перевести CTRL в ШИМ-режим.

        frequency_khz:
            100..50000 кГц, шаг 10 кГц.

        width_ns:
            ширина положительного импульса, шаг 10 нс.
            Должна быть не меньше 10 нс и не больше period_ns - 10 нс.
        """
        if not isinstance(frequency_khz, int) or isinstance(frequency_khz, bool):
            raise TypeError("frequency_khz must be int")
        if not isinstance(width_ns, int) or isinstance(width_ns, bool):
            raise TypeError("width_ns must be int")

        if not (
            self.CTRL_PWM_MIN_FREQUENCY_KHZ
            <= frequency_khz
            <= self.CTRL_PWM_MAX_FREQUENCY_KHZ
        ):
            raise ValueError(
                "frequency_khz must be in range "
                f"{self.CTRL_PWM_MIN_FREQUENCY_KHZ}.."
                f"{self.CTRL_PWM_MAX_FREQUENCY_KHZ}"
            )

        if frequency_khz % self.CTRL_PWM_FREQUENCY_STEP_KHZ != 0:
            raise ValueError(
                f"frequency_khz must be a multiple of "
                f"{self.CTRL_PWM_FREQUENCY_STEP_KHZ} kHz"
            )

        if width_ns < self.CTRL_PWM_WIDTH_STEP_NS:
            raise ValueError(
                f"width_ns must be >= {self.CTRL_PWM_WIDTH_STEP_NS} ns"
            )

        if width_ns % self.CTRL_PWM_WIDTH_STEP_NS != 0:
            raise ValueError(
                f"width_ns must be a multiple of "
                f"{self.CTRL_PWM_WIDTH_STEP_NS} ns"
            )

        period_ns = 1_000_000.0 / frequency_khz
        max_width_ns = period_ns - self.CTRL_PWM_WIDTH_STEP_NS

        if width_ns > max_width_ns:
            raise ValueError(
                f"width_ns={width_ns} is too large for F={frequency_khz} kHz. "
                f"Period is {period_ns:g} ns, maximum allowed width is "
                f"{max_width_ns:g} ns."
            )

        cmd = f"SET_CTRL_PIN F={frequency_khz} W={width_ns}".encode("ascii")
        response = self._send_command(cmd)

        if not self._check_ok(response):
            logger.error(
                "SET_CTRL_PIN PWM error: F=%d kHz, W=%d ns, response=%r",
                frequency_khz,
                width_ns,
                response,
            )
            return False

        logger.debug("CTRL PWM: F=%d kHz, W=%d ns", frequency_khz, width_ns)
        return True

    @staticmethod
    def ctrl_pwm_real_frequency_khz(frequency_khz: int) -> float:
        """Рассчитать реальную частоту CTRL PWM по формуле из MGPDLab."""
        if frequency_khz <= 0:
            raise ValueError("frequency_khz must be positive")

        divider = 100_000 // frequency_khz
        if divider <= 0:
            raise ValueError("frequency_khz is too high")

        return 100_000 / divider

    # Alias with the protocol command terminology.
    set_ctrl_pin = set_ctrl

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()