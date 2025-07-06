"""
Audio Device Monitor для Windows
Модуль для мониторинга изменений аудио-устройств (подключение/отключение) 
без перезагрузки программы.

Использует эффективное сравнение списков устройств через sounddevice.
"""
import threading
import time
import logging
import hashlib
from typing import Callable, Optional, Dict, Set
import sounddevice as sd

# Настройка логирования
logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class AudioDeviceInfo:
    """Информация об аудио-устройстве."""
    
    def __init__(self, index: int, name: str, channels: int, is_default: bool = False):
        self.index = index
        self.name = name
        self.channels = channels
        self.is_default = is_default
        self.hash_key = self._generate_hash()
    
    def _generate_hash(self) -> str:
        """Генерирует уникальный хеш для устройства."""
        # Используем только имя для хеша, так как индекс может изменяться
        return hashlib.md5(f"{self.name}".encode()).hexdigest()
    
    def __eq__(self, other):
        return isinstance(other, AudioDeviceInfo) and self.hash_key == other.hash_key
    
    def __hash__(self):
        return hash(self.hash_key)
    
    def __str__(self):
        return f"{self.name} ({self.channels} ch) [Index: {self.index}]"


class AudioDeviceMonitor:
    """
    Основной класс для мониторинга изменений аудио-устройств.
    Использует эффективное отслеживание через sounddevice.
    """
    
    def __init__(self, device_change_callback: Optional[Callable] = None):
        """
        Инициализация монитора аудио-устройств.
        
        Args:
            device_change_callback: Функция обратного вызова для обработки изменений устройств
        """
        self.device_change_callback = device_change_callback
        self.is_monitoring = False
        self.monitor_thread = None
        self._stop_event = threading.Event()
        self.previous_devices: Set[AudioDeviceInfo] = set()
        self.check_interval = 0.8  # Интервал проверки в секундах (более частая проверка)
        
    def start(self):
        """Запускает мониторинг аудио-устройств."""
        if self.is_monitoring:
            logger.warning("Мониторинг уже запущен")
            return
            
        try:
            # Получаем начальный список устройств
            self.previous_devices = self._get_current_devices()
            logger.info(f"Начальный список устройств: {len(self.previous_devices)}")
            
            self.is_monitoring = True
            self._stop_event.clear()
            
            # Запускаем поток мониторинга
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
            
            logger.info("Мониторинг аудио-устройств запущен")
            
        except Exception as e:
            logger.error(f"Ошибка запуска мониторинга: {e}")
            self.is_monitoring = False
            raise
    
    def stop(self):
        """Останавливает мониторинг аудио-устройств."""
        if not self.is_monitoring:
            return
            
        try:
            self._stop_event.set()
            self.is_monitoring = False
            
            if self.monitor_thread and self.monitor_thread.is_alive():
                self.monitor_thread.join(timeout=5.0)
            
            logger.info("Мониторинг аудио-устройств остановлен")
            
        except Exception as e:
            logger.error(f"Ошибка остановки мониторинга: {e}")
    
    def _monitor_loop(self):
        """Основной цикл мониторинга устройств."""
        cycle_count = 0
        while not self._stop_event.wait(self.check_interval):
            try:
                cycle_count += 1
                current_devices = self._get_current_devices()
                
                # Отладочная информация каждые 10 циклов
                if cycle_count % 10 == 0:
                    logger.debug(f"Мониторинг цикл {cycle_count}: текущих устройств {len(current_devices)}")
                
                # Проверяем добавленные устройства
                added_devices = current_devices - self.previous_devices
                if added_devices:
                    for device in added_devices:
                        logger.info(f"🔌 Обнаружено новое устройство: {device}")
                        self._handle_device_change('device_added', device)
                
                # Проверяем удаленные устройства  
                removed_devices = self.previous_devices - current_devices
                if removed_devices:
                    for device in removed_devices:
                        logger.info(f"🔌 Устройство удалено: {device}")
                        self._handle_device_change('device_removed', device)
                
                # Обновляем список устройств
                self.previous_devices = current_devices
                
            except Exception as e:
                logger.error(f"❌ Ошибка в цикле мониторинга: {e}")
                time.sleep(3.0)  # Увеличиваем интервал при ошибке
    
    def _get_current_devices(self) -> Set[AudioDeviceInfo]:
        """
        Получает текущий список аудио-устройств.
        
        Returns:
            Set[AudioDeviceInfo]: Множество устройств вывода звука
        """
        devices = set()
        
        try:
            # Принудительно обновляем список устройств
            try:
                sd._terminate()
                sd._initialize()
            except:
                pass
            
            # Получаем список через sounddevice
            device_list = sd.query_devices()
            
            # Получаем устройство по умолчанию
            default_device = None
            try:
                default_device = sd.default.device[1]  # Устройство вывода
            except:
                pass
            
            device_count = 0
            for device in device_list:
                if isinstance(device, dict):
                    device_index = device.get('index', -1)
                    device_name = device.get('name', 'Unknown Device').strip()
                    max_outputs = device.get('max_output_channels', 0)
                    
                    # Включаем только устройства вывода звука
                    if max_outputs > 0:
                        device_count += 1
                        is_default = (device_index == default_device)
                        
                        # Создаем более простой идентификатор устройства
                        audio_device = AudioDeviceInfo(
                            index=device_index,
                            name=device_name,
                            channels=max_outputs,
                            is_default=is_default
                        )
                        devices.add(audio_device)
            
            # Отладочная информация для первых нескольких вызовов
            if len(devices) != getattr(self, '_last_device_count', 0):
                logger.info(f"📊 Количество устройств изменилось: {getattr(self, '_last_device_count', 0)} → {len(devices)}")
                self._last_device_count = len(devices)
                        
        except Exception as e:
            logger.error(f"❌ Ошибка получения списка устройств: {e}")
        
        return devices
    
    def _handle_device_change(self, event_type: str, device_info: AudioDeviceInfo):
        """
        Обрабатывает изменения устройств.
        
        Args:
            event_type: Тип события ('device_added', 'device_removed', etc.)
            device_info: Информация об устройстве
        """
        try:
            if self.device_change_callback:
                self.device_change_callback(event_type, device_info)
        except Exception as e:
            logger.error(f"Ошибка в callback функции: {e}")
    
    def get_current_audio_devices(self) -> Set[AudioDeviceInfo]:
        """
        Получает текущий список аудио-устройств для немедленного использования.
        
        Returns:
            Set[AudioDeviceInfo]: Множество устройств
        """
        return self._get_current_devices()
    
    def get_device_details(self) -> Dict[str, dict]:
        """
        Получает детальную информацию о всех аудио-устройствах.
        
        Returns:
            Dict[str, dict]: Словарь с детальной информацией
        """
        details = {}
        
        try:
            devices = self._get_current_devices()
            for device in devices:
                details[device.name] = {
                    'index': device.index,
                    'name': device.name,
                    'channels': device.channels,
                    'is_default': device.is_default,
                    'hash': device.hash_key
                }
        except Exception as e:
            logger.error(f"Ошибка получения детальной информации: {e}")
        
        return details
    
    def __enter__(self):
        """Поддержка контекстного менеджера."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Поддержка контекстного менеджера."""
        self.stop()


# Пример использования
if __name__ == "__main__":
    def device_change_handler(event_type: str, device_info: AudioDeviceInfo):
        """Пример обработчика изменений устройств."""
        print(f"🔔 Событие: {event_type}")
        print(f"📱 Устройство: {device_info}")
        print("-" * 50)
    
    # Использование как контекстного менеджера
    with AudioDeviceMonitor(device_change_handler) as monitor:
        print("Мониторинг аудио-устройств запущен.")
        print("Подключите или отключите аудио-устройство для проверки.")
        print("Нажмите Ctrl+C для остановки...")
        
        # Показываем начальный список устройств
        initial_devices = monitor.get_current_audio_devices()
        print(f"\nНачальный список устройств ({len(initial_devices)}):")
        for device in initial_devices:
            print(f"  • {device}")
        print()
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nОстановка мониторинга...")