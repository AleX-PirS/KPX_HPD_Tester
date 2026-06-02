import socket
import logging

logger = logging.getLogger(__name__)

class MGPDClient:
    """
    Синхронный клиент для управления оборудованием через протокол MGPD.
    При подключении автоматически активирует KIPIX CONTROL (запись 0xA5 в 0x8031).
    """
    
    def __init__(self, host='127.0.0.1', port=0xBEEB, timeout=5.0, auto_enable_kipix=True):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.auto_enable_kipix = auto_enable_kipix
        self._socket = None
        self._connected = False

    def connect(self):
        """Устанавливает соединение и, если нужно, активирует KIPIX CONTROL."""
        if self._connected:
            self.disconnect()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.settimeout(self.timeout)
        try:
            self._socket.connect((self.host, self.port))
            self._connected = True
            logger.info(f"Connected to {self.host}:{self.port}")
            
            if self.auto_enable_kipix:
                if not self.write_byte(0xA5, 0x8031):
                    raise RuntimeError("Failed to enable KIPIX CONTROL (write 0xA5 to 0x8031)")
                logger.info("KIPIX CONTROL enabled")
                
        except Exception as e:
            if self._socket:
                self._socket.close()
                self._socket = None
            self._connected = False
            raise ConnectionError(f"Failed to connect or enable KIPIX: {e}")

    def disconnect(self):
        """Закрывает соединение."""
        if self._socket:
            self._socket.close()
            self._socket = None
        self._connected = False
        logger.info("Disconnected")

    def _send_command(self, cmd: bytes) -> bytes:
        """Отправляет команду и возвращает ответ сервера."""
        if not self._connected:
            raise RuntimeError("Not connected. Call connect() first.")
        self._socket.sendall(cmd)
        # В текущем протоколе ответ всегда умещается в 1024 байта
        return self._socket.recv(1024)

    def _check_error(self, response: bytes) -> bool:
        """Возвращает True, если в ответе есть 'ERROR'."""
        return b'ERROR' in response

    def write_byte(self, value: int, address: int) -> bool:
        """
        Записывает байт по указанному адресу.
        Возвращает True при успехе, False при ошибке.
        """
        cmd = f"WRITE_BYTE 0x{value:02X} TO 0x{address:04X}\r\n".encode()
        resp = self._send_command(cmd)
        if self._check_error(resp):
            logger.error(f"Write error: addr=0x{address:04X}, val=0x{value:02X}, response={resp}")
            return False
        logger.debug(f"Write OK: addr=0x{address:04X}, val=0x{value:02X}")
        return True

    def read_byte(self, address: int) -> int | None:
        """
        Читает байт из указанного адреса.
        Возвращает целое число (0..255) или None при ошибке.
        """
        cmd = f"READ_BYTE FROM 0x{address:04X}\r\n".encode()
        resp = self._send_command(cmd)
        if self._check_error(resp):
            logger.error(f"Read error: addr=0x{address:04X}, response={resp}")
            return None
        # Ожидаем ответ вида "0x55" или "55"
        try:
            resp_str = resp.decode('ascii').strip()
            if resp_str.startswith('0x'):
                return int(resp_str, 16)
            else:
                return int(resp_str, 16)
        except ValueError:
            logger.error(f"Failed to parse response: {resp_str}")
            return None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()