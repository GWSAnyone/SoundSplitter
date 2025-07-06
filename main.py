import flet as ft
import sounddevice as sd
import collections
import threading
import os
import json
import time
import numpy as np
# from application_audio_router import ApplicationAudioRouter  # Отключено
import asyncio
from audio_device_monitor import AudioDeviceMonitor


class SettingsManager:
    def __init__(self, filepath='device_settings.json'):
        self.filepath = filepath
        self.settings = {
            "device_settings": {},
            "dont_show_save_notification": False,
            "theme": "light"
        }

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    self.settings = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                pass
        return self.settings

    def save(self, settings=None):
        if settings is None:
            settings = self.settings
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка сохранения настроек: {e}")


class AudioForwarderApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.settings_manager = SettingsManager()
        self.settings = self.settings_manager.load()
        self.is_dark_mode = self.settings.get("theme", "light") == "dark"
        self.language = self.settings.get("language", 'ru')

        self.setup_page()
        self.initialize_state()
        self.setup_ui()
        self.load_settings()
        self.device_containers = {}
        self.update_devices()

        # self.audio_router = ApplicationAudioRouter(self.target_devices_list, self)  # Отключено
        self.source_device_name = None

        self.apply_theme()
        
        # Запускаем таймер обновления статуса
        self.start_status_timer()



    def load_settings(self):
        loaded_settings = self.settings_manager.load()
        self.device_settings = loaded_settings.get("device_settings", {})
        
        # Загружаем аудио настройки
        self.sample_rate = loaded_settings.get("sample_rate", 48000)
        self.blocksize = loaded_settings.get("blocksize", 256)
        
        # Обновляем UI элементы если они уже созданы
        if hasattr(self, 'sample_rate_dropdown'):
            self.sample_rate_dropdown.value = str(self.sample_rate)
        if hasattr(self, 'blocksize_dropdown'):
            self.blocksize_dropdown.value = str(self.blocksize)

    def save_settings(self):
        """Сохраняет текущие настройки устройств."""
        for device in self.target_devices_list:
            self.device_settings[device] = {
                'delay': self.delays.get(device, 0),
                'volume': self.volumes.get(device, 0)
            }

        self.settings_manager.settings["device_settings"] = self.device_settings
        self.settings_manager.save(self.settings_manager.settings)

    def start_status_timer(self):
        """Запускает таймер обновления статуса."""
        import threading
        self.status_timer = threading.Timer(1.0, self.update_status)
        self.status_timer.daemon = True
        self.status_timer.start()
    
    def _reset_statistics(self):
        """Сбрасывает статистику потоков."""
        self.stream_stats = {
            'active_streams': 0,
            'total_frames': 0,
            'errors_count': 0,
            'start_time': None,
            'total_callbacks': 0,
            'data_processed_mb': 0.0,
            'last_callback_time': 0,
            'callback_intervals': collections.deque(maxlen=100)
        }
        print("📊 Статистика потоков сброшена")

    def update_status(self):
        """Обновляет статус-бар с правильной статистикой."""
        try:
            current_time = time.time()
            
            # DEBUG: периодически выводим информацию о работе
            if not hasattr(self, '_debug_counter'):
                self._debug_counter = 0
            self._debug_counter += 1
            
            if self._debug_counter % 10 == 0:  # Каждые 5 секунд
                print(f"🔄 Обновление статуса #{self._debug_counter}, потоков: {len(self.device_streams)}")
                # Принудительно обновляем устройства каждые 5 секунд для проверки
                self._force_device_update = True
                self.update_devices()
            
            # Throttling: обновляем UI не чаще чем раз в 100ms
            if current_time - self.last_ui_update < self.ui_update_throttle:
                self.pending_ui_updates = True
                return
            
            # ИСПРАВЛЕНО: правильный подсчет активных потоков
            active_streams = 0
            debug_info = []
            
            for device_name, streams in self.device_streams.items():
                if streams:
                    input_stream, output_stream = streams
                    is_active = False
                    
                    # Считаем поток активным если output_stream работает
                    if output_stream and hasattr(output_stream, 'active') and output_stream.active:
                        active_streams += 1
                        is_active = True
                        debug_info.append(f"{device_name}: active=True")
                    # Альтернативная проверка для разных типов потоков
                    elif output_stream and not getattr(output_stream, 'closed', True):
                        active_streams += 1
                        is_active = True
                        debug_info.append(f"{device_name}: not_closed=True")
                    else:
                        debug_info.append(f"{device_name}: inactive")
                        
            # DEBUG: выводим детальную информацию о потоках
            if self._debug_counter % 10 == 0 and debug_info:
                print(f"📊 Детали потоков: {'; '.join(debug_info)}")
            
            self.streams_indicator.value = f"Потоки: {active_streams}"
            
            # ИСПРАВЛЕНО: понятная статистика производительности  
            if self.stream_stats['start_time'] and self.stream_stats['total_callbacks'] > 0:
                elapsed = current_time - self.stream_stats['start_time']
                
                # Callback'и в секунду (реальная частота обработки)
                callbacks_per_sec = self.stream_stats['total_callbacks'] / elapsed if elapsed > 0 else 0
                
                # Обработанные данные в МБ/сек
                data_rate_mb = self.stream_stats['data_processed_mb'] / elapsed if elapsed > 0 else 0
                
                # Стабильность (разброс интервалов между callback'ами)
                stability = "Стабильно"
                if len(self.stream_stats['callback_intervals']) > 10:
                    intervals = list(self.stream_stats['callback_intervals'])
                    avg_interval = sum(intervals) / len(intervals)
                    max_deviation = max(abs(i - avg_interval) for i in intervals)
                    if max_deviation > avg_interval * 0.5:  # Отклонение больше 50%
                        stability = "Нестабильно"
                
                self.performance_indicator.value = f"{callbacks_per_sec:.0f} call/s | {data_rate_mb:.1f} MB/s | {stability}"
                
                # DEBUG: статистика callback'ов
                if self._debug_counter % 10 == 0:
                    print(f"📈 Статистика: {self.stream_stats['total_callbacks']} callback'ов за {elapsed:.1f}с")
            else:
                self.performance_indicator.value = f"Статистика собирается..."
                # DEBUG: почему нет статистики
                if self._debug_counter % 10 == 0:
                    print(f"⚠️ Нет статистики: start_time={self.stream_stats['start_time']}, callbacks={self.stream_stats['total_callbacks']}")
            
            # Обновляем информацию об ошибках с процентом
            errors = self.stream_stats['errors_count']
            total_calls = max(1, self.stream_stats['total_callbacks'])
            error_rate = (errors / total_calls) * 100
            
            self.error_indicator.value = f"Ошибки: {errors} ({error_rate:.1f}%)"
            
            # ИСПРАВЛЕНО: более точный статус трансляции
            is_transmitting = (self.transmission_thread and self.transmission_thread.is_alive() and 
                             active_streams > 0)
            
            if is_transmitting:
                self.status_text.value = f"▶️ Транслирую на {active_streams} устройств"
            elif self.transmission_thread and self.transmission_thread.is_alive():
                self.status_text.value = "⚠️ Поток запущен, но нет целей"
            else:
                self.status_text.value = "⏸️ Готов к работе"
            
            # Обновляем UI только если есть изменения
            if hasattr(self, 'page') and (self.pending_ui_updates or current_time - self.last_ui_update > 1.0):
                self.page.update()
                self.last_ui_update = current_time
                self.pending_ui_updates = False
                
                # DEBUG: подтверждение обновления UI
                if self._debug_counter % 10 == 0:
                    print(f"🖥️ UI обновлен успешно")
            
        except Exception as e:
            print(f"⚠️ Ошибка обновления статуса: {e}")
        
        # Планируем следующее обновление
        if not self.stop_event.is_set():  # Проверяем что приложение не закрывается
            self.status_timer = threading.Timer(0.5, self.update_status)
            self.status_timer.daemon = True
            self.status_timer.start()

    def setup_page(self):
        """Initial page setup."""
        self.page.title = "🎵 Audio Forwarder"
        self.page.window.on_event = self.window_event_handler
        self.page.window.prevent_close = True
        self.page.scroll = ft.ScrollMode.AUTO
        self.page.window.width = 1000
        self.page.window.height = 890
        self.page.window.min_width = 450
        self.page.window.min_height = 890

    def initialize_state(self):
        """Initialize state variables."""
        self.transmission_thread = None
        self.stop_event = threading.Event()
        self.target_devices_list = []
        self.delays = {}
        self.buffers = {}
        self.device_streams = {}
        self.volumes = {}
        self.device_settings = {}
        
        # Аудио параметры для качественного воспроизведения
        self.sample_rate = 48000  # Высокое качество
        self.blocksize = 256      # Низкая задержка
        self.bit_depth = 'float32'
        
        # Кеш для устройств
        self.devices_cache = {}
        self.devices_cache_time = 0
        self.cache_timeout = 5.0  # Обновлять кеш каждые 5 секунд
        self._force_device_update = False  # Флаг принудительного обновления
        
        # Статистика потоков (улучшенная)
        self.stream_stats = {
            'active_streams': 0,
            'total_frames': 0,
            'errors_count': 0,
            'start_time': None,
            'total_callbacks': 0,
            'data_processed_mb': 0.0,
            'last_callback_time': 0,
            'callback_intervals': collections.deque(maxlen=100)  # Для измерения стабильности
        }
        
        # Оптимизация производительности
        self.ui_update_throttle = 0.1  # Ограничиваем обновления UI до 10 раз в секунду
        self.last_ui_update = 0
        self.pending_ui_updates = False
        
        # Управление памятью
        self.memory_cleanup_counter = 0
        self.memory_cleanup_interval = 1000  # Очистка каждые 1000 callback вызовов
        
        # Автоматическое восстановление
        self.recovery_attempts = 0
        self.max_recovery_attempts = 3
        self.last_error_time = 0
        self.error_recovery_delay = 5.0  # секунд
        
        # Защита от аудио-петель (критично для Bluetooth устройств)
        self.loop_protection_enabled = True
        self.loop_detection_buffer = collections.deque([0.0], maxlen=100)  # Буфер для анализа петель (float значения)
        self.loop_detection_threshold = 0.95  # Порог корреляции для определения петли
        self.loop_prevention_enabled = True
        self.problematic_devices = set()  # Список проблемных устройств
        self.loop_protection_stats = {
            'loops_detected': 0,
            'loops_prevented': 0,
            'false_positives': 0,
            'last_loop_time': 0
        }
        
        # Отладка задержки - дополнительное деление на 1000 если нужно
        self.delay_debug_mode = False  # Установить True если задержки все еще неправильные
        self._delay_debug_printed = set()  # Для отслеживания диагностических сообщений

    def setup_ui(self):
        """Set up the user interface."""
        # Source and target device selection with full width
        self.source_combo = ft.Dropdown(
            label="Источник звука", 
            options=[],
            expand=True,
            border_radius=10,
            on_change=self.on_source_device_change
        )
        self.target_combo = ft.Dropdown(
            label="Целевые устройства", 
            options=[],
            expand=True,
            border_radius=10
        )

        # Buttons with consistent styling that adapts to theme
        self.language_toggle_button = ft.ElevatedButton(
            text="Рус" if self.language == 'ru' else "Eng",
            on_click=self.toggle_language,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=10)
            )
        )

        self.advanced_settings_button = ft.ElevatedButton(
            text="Расширенные настройки",
            on_click=lambda _: self.on_advanced_settings_click(),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=10)
            )
        )

        self.theme_toggle_button = ft.ElevatedButton(
            text="День" if not self.is_dark_mode else "Ночь",
            on_click=self.toggle_theme,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=10)
            )
        )

        self.theme_lang_buttons = ft.Row(
            [self.theme_toggle_button, self.language_toggle_button],
            spacing=10
        )

        # Настройки качества звука
        self.sample_rate_dropdown = ft.Dropdown(
            label="Sample Rate",
            options=[
                ft.dropdown.Option("44100", "44.1 kHz (CD качество)"),
                ft.dropdown.Option("48000", "48 kHz (студийное)"),
                ft.dropdown.Option("96000", "96 kHz (Hi-Res)"),
                ft.dropdown.Option("192000", "192 kHz (Max)")
            ],
            value=str(self.sample_rate),
            width=200,
            on_change=self.on_sample_rate_change,
            tooltip="Частота дискретизации влияет на качество звука"
        )

        self.blocksize_dropdown = ft.Dropdown(
            label="Buffer Size",
            options=[
                ft.dropdown.Option("64", "64 (мин. задержка)"),
                ft.dropdown.Option("128", "128 (низкая задержка)"),
                ft.dropdown.Option("256", "256 (рекомендуется)"),
                ft.dropdown.Option("512", "512 (стабильность)"),
                ft.dropdown.Option("1024", "1024 (макс. стабильность)")
            ],
            value=str(self.blocksize),
            width=200,
            on_change=self.on_blocksize_change,
            tooltip="Размер буфера: меньше = меньше задержка, больше = стабильнее"
        )

        self.audio_settings_row = ft.Row(
            [self.sample_rate_dropdown, self.blocksize_dropdown],
            spacing=10
        )

        # Статус-бар
        self.status_text = ft.Text(
            "Готов к работе",
            size=12,
            weight=ft.FontWeight.BOLD
        )
        
        self.streams_indicator = ft.Text(
            "Потоки: 0",
            size=12
        )
        
        self.performance_indicator = ft.Text(
            "Производительность: --",
            size=12
        )
        
        self.error_indicator = ft.Text(
            "Ошибки: 0",
            size=12
        )
        
        self.status_bar = ft.Container(
            content=ft.Row([
                self.status_text,
                ft.VerticalDivider(width=1, color="gray"),
                self.streams_indicator,
                ft.VerticalDivider(width=1, color="gray"),
                self.performance_indicator,
                ft.VerticalDivider(width=1, color="gray"),
                self.error_indicator
            ], spacing=15),
            bgcolor="surface",
            border=ft.border.all(1, "gray"),
            border_radius=5,
            padding=10,
            margin=5
        )

        # Control buttons with consistent styling
        self.restart_button = ft.ElevatedButton(
            text="Перезапустить",
            on_click=lambda _: self.restart_capture(),
            disabled=True,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=10)
            )
        )
        
        self.start_button = ft.ElevatedButton(
            text="Запустить",
            on_click=lambda _: self.start_capture(),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=10)
            )
        )
        
        self.stop_button = ft.ElevatedButton(
            text="Остановить",
            on_click=lambda _: self.stop_capture(),
            disabled=True,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=10)
            )
        )

        self.Start_Stop_button = ft.Row(
            [self.start_button, self.stop_button, self.restart_button],
            alignment=ft.MainAxisAlignment.START,
            spacing=10
        )

        self.Start_Stop_Voice_buttons = ft.Row(
            [self.Start_Stop_button, self.advanced_settings_button],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN
        )

        # Add and other control buttons with consistent styling
        self.add_button = ft.ElevatedButton(
            text="Добавить",
            on_click=lambda _: self.add_device(self.target_combo.value),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=10)
            )
        )

        # Device control buttons with consistent styling
        self.refresh_devices_button = ft.ElevatedButton(
            text="🔄 Обновить устройства",
            on_click=lambda _: self.force_refresh_devices(),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=10)
            ),
            tooltip="Принудительно обновить список аудио-устройств\n(недоступно при активных потоках)"
        )

        self.diagnose_devices_button = ft.ElevatedButton(
            text="🔍 Диагностика устройств",
            on_click=lambda _: self.diagnose_audio_devices(),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=10)
            ),
            tooltip="Анализ проблемных устройств и аудио-петель"
        )

        self.device_control_buttons = ft.Row(
            [self.add_button, self.refresh_devices_button, self.diagnose_devices_button],
            spacing=10
        )

        self.add_theme_buttons = ft.Row(
            [self.device_control_buttons, self.theme_lang_buttons],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN
        )
        
        self.clear_button = ft.ElevatedButton(
            text="Очистить список",
            on_click=lambda _: self.clear_devices(),
            visible=False,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=10)
            )
        )

        # Selected devices list with scrollable panel
        self.selected_devices_list = ft.Row(
            wrap=True, spacing=10, run_spacing=10, expand=False
        )
        
        self.devices_panel = ft.Container(
            content=self.selected_devices_list,
            border=ft.border.all(2, "blue"),
            padding=15, margin=15,
            border_radius=15,
            visible=False,
            bgcolor="surface"
        )

        # Add elements to the page
        self.page.add(
            ft.Container(
                content=ft.Column([
                    ft.Text(
                        "🎵 Audio Forwarder", 
                        size=28, 
                        weight=ft.FontWeight.BOLD,
                        text_align=ft.TextAlign.CENTER
                    ),
                    ft.Divider(height=20, thickness=2),
                    self.source_combo,
                    self.target_combo,
                    ft.Text("Настройки качества звука:", weight=ft.FontWeight.BOLD),
                    self.audio_settings_row,
                    self.add_theme_buttons,
                    self.Start_Stop_Voice_buttons,
                    self.devices_panel,
                    self.clear_button,
                    self.status_bar,
                ], spacing=15),
                padding=20
            )
        )

    def apply_theme(self):
        """Apply the current theme to the page."""
        if self.is_dark_mode:
            self.page.theme_mode = ft.ThemeMode.DARK
            self.theme_toggle_button.text = self.get_translation("Ночь")
        else:
            self.page.theme_mode = ft.ThemeMode.LIGHT
            self.theme_toggle_button.text = self.get_translation("День")

        self.update_texts()
        self.page.update()

    def toggle_theme(self, _):
        """Toggle between dark and light themes."""
        self.is_dark_mode = not self.is_dark_mode
        self.settings["theme"] = "dark" if self.is_dark_mode else "light"
        self.settings_manager.save(self.settings)
        self.apply_theme()

    def on_sample_rate_change(self, e):
        """Обработка изменения частоты дискретизации."""
        if self.transmission_thread and self.transmission_thread.is_alive():
            self.show_message("Остановите трансляцию перед изменением настроек аудио")
            e.control.value = str(self.sample_rate)  # Откатываем изменение
            self.page.update()
            return
        
        self.sample_rate = int(e.control.value)
        self.settings["sample_rate"] = self.sample_rate
        self.settings_manager.save(self.settings)
        print(f"🎵 Sample rate изменен на: {self.sample_rate} Hz")
        
        # Сбрасываем статистику и диагностику
        self._reset_statistics()
        self._delay_debug_printed.clear()
        self.delay_debug_mode = False

    def on_blocksize_change(self, e):
        """Обработка изменения размера буфера."""
        if self.transmission_thread and self.transmission_thread.is_alive():
            self.show_message("Остановите трансляцию перед изменением настроек аудио")
            e.control.value = str(self.blocksize)  # Откатываем изменение
            self.page.update()
            return
        
        self.blocksize = int(e.control.value)
        self.settings["blocksize"] = self.blocksize
        self.settings_manager.save(self.settings)
        print(f"🔧 Buffer size изменен на: {self.blocksize} frames")
        
        # Сбрасываем статистику и диагностику
        self._reset_statistics()
        self._delay_debug_printed.clear()
        self.delay_debug_mode = False

    def on_source_device_change(self, e):
        """Handle source device change"""
        if e.control.value:
            print(f"🎤 Источник звука изменен: {e.control.value}")
            
            # ИСПРАВЛЕНИЕ: Обновляем источник в ApplicationAudioRouter
            # if hasattr(self, 'audio_router') and self.audio_router:
            #     self.audio_router.update_source_device(e.control.value)  # Отключено
            
            self.save_settings()
            
            # Если трансляция активна, перезапускаем с новым источником
            if self.transmission_thread and self.transmission_thread.is_alive():
                self.show_message("⚠️ Источник звука изменен. Перезапуск трансляции...")
                self.restart_capture()
            else:
                pass

    def on_routing_settings_changed(self, app_name, selected_devices):
        """Обработка изменений настроек маршрутизации."""
        print(f"🔄 Настройки маршрутизации изменены для {app_name}: {selected_devices}")
        
        # Если трансляция активна, обновляем маршрутизацию в реальном времени
        if self.transmission_thread and self.transmission_thread.is_alive():
            print("📡 Обновление маршрутизации в реальном времени...")
            # Маршрутизация будет обновлена при следующем callback'е
            
    def should_route_to_device(self, device_name):
        """Проверяет должен ли звук маршрутизироваться на указанное устройство."""
        # if not hasattr(self, 'audio_router') or not self.audio_router:
        return True  # Если нет маршрутизатора, разрешаем все устройства (отключено)
        
        # return self.audio_router.should_route_to_device(device_name)  # Отключено

    def force_refresh_devices(self):
        """Принудительное обновление списка устройств через AudioDeviceMonitor."""
        print("🔄 Принудительное обновление устройств...")
        
        # Проверяем активные потоки
        is_streaming = (self.transmission_thread and 
                       self.transmission_thread.is_alive() and 
                       len(self.device_streams) > 0)
        
        if is_streaming:
            self.show_message("⚠️ Обновление устройств недоступно!\n\n"
                            "Сначала остановите активные потоки аудио, "
                            "затем попробуйте обновить список устройств.")
            print("⚠️ Обновление заблокировано - активны потоки")
            return
        
        try:
            # Используем AudioDeviceMonitor для получения актуального списка
            device_monitor = AudioDeviceMonitor()
            
            # Получаем текущий список устройств
            current_devices = device_monitor.get_current_audio_devices()
            device_details = device_monitor.get_device_details()
            
            print(f"📊 Обнаружено {len(current_devices)} аудио-устройств:")
            for device in current_devices:
                print(f"  • {device}")
            
            # Принудительно обновляем кеш устройств
            self._force_device_update = True
            self.devices_cache.clear()
            self.devices_cache_time = 0
            
            # Обновляем список устройств в UI
            self.update_devices()
            
            # Показываем результат пользователю (отложенно)
            message = f"✅ Обновление завершено!\n\n"
            message += f"Найдено {len(current_devices)} аудио-устройств.\n"
            message += f"Список устройств успешно обновлен."
            
            print("✅ Принудительное обновление завершено успешно")
            print(f"📝 {message}")
            
            # Отложенное показ сообщения чтобы избежать конфликтов UI
            import threading
            def delayed_message():
                import time
                time.sleep(0.5)  # Ждем завершения текущих обновлений UI
                self.show_message(message)
            
            threading.Thread(target=delayed_message, daemon=True).start()
            
        except Exception as e:
            error_msg = f"❌ Ошибка обновления устройств: {e}"
            print(error_msg)
            
            # Отложенное показ ошибки
            import threading
            def delayed_error():
                import time
                time.sleep(0.5)
                self.show_message(error_msg)
            
            threading.Thread(target=delayed_error, daemon=True).start()

    def diagnose_audio_devices(self):
        """
        Диагностика аудио-устройств для выявления проблемных устройств и аудио-петель.
        Особенно полезна для Bluetooth устройств как Tronsmart Element T6.
        """
        print("\n" + "="*70)
        print("🔍 ДИАГНОСТИКА АУДИО-УСТРОЙСТВ")
        print("="*70)
        
        try:
            devices = sd.query_devices()
            host_apis = sd.query_hostapis()
            
            # Создаем индекс host API для быстрого доступа
            host_api_names = {}
            for i, api in enumerate(host_apis):
                try:
                    host_api_names[i] = api.get('name', 'Unknown') if hasattr(api, 'get') else str(api) # type: ignore
                except:
                    host_api_names[i] = 'Unknown'
            
            print(f"\n📊 Обнаружено {len(devices)} аудио-устройств")
            print(f"🌐 Доступно {len(host_apis)} аудио-интерфейсов")
            
            # Анализируем каждое устройство
            problematic_devices = []
            bluetooth_devices = []
            loop_risk_devices = []
            
            for device in devices:
                try:
                    name = device.get('name', 'Unknown') if hasattr(device, 'get') else str(device) # type: ignore
                    device_id = device.get('index', -1) if hasattr(device, 'get') else -1 # type: ignore
                    max_input = device.get('max_input_channels', 0) if hasattr(device, 'get') else 0 # type: ignore
                    max_output = device.get('max_output_channels', 0) if hasattr(device, 'get') else 0 # type: ignore
                    hostapi_id = device.get('hostapi', -1) if hasattr(device, 'get') else -1 # type: ignore
                    default_samplerate = device.get('default_samplerate', 0) if hasattr(device, 'get') else 0 # type: ignore
                    
                    host_api_name = host_api_names.get(hostapi_id, 'Unknown')
                    
                    # Проверяем на Tronsmart Element T6 или похожие устройства
                    is_tronsmart = 'tronsmart' in name.lower() or 'element' in name.lower() or 't6' in name.lower()
                    is_bluetooth = any(keyword in name.lower() for keyword in ['bluetooth', 'bt', 'wireless', 'headphones', 'speakers'])
                    
                    # Детальная диагностика
                    print(f"\n🔍 Устройство #{device_id}: {name}")
                    print(f"  📡 API: {host_api_name}")
                    print(f"  🎤 Вход: {max_input} каналов")
                    print(f"  🔊 Выход: {max_output} каналов")
                    print(f"  ⚡ Частота: {default_samplerate} Hz")
                    
                    # КРИТИЧЕСКАЯ ПРОВЕРКА: устройство с входом И выходом
                    if max_input > 0 and max_output > 0:
                        print(f"  ⚠️  РИСК АУДИО-ПЕТЛИ: устройство может принимать И воспроизводить звук!")
                        loop_risk_devices.append(name)
                        
                        # Особенно опасно для Bluetooth устройств
                        if is_bluetooth:
                            print(f"  🚨 BLUETOOTH + ДВУНАПРАВЛЕННОСТЬ = ВЫСОКИЙ РИСК ПЕТЛИ!")
                            problematic_devices.append(name)
                    
                    # Проверка на Tronsmart Element T6
                    if is_tronsmart:
                        print(f"  🎯 НАЙДЕН TRONSMART ELEMENT T6!")
                        bluetooth_devices.append(name)
                        
                        # Проверяем доступность устройства
                        try:
                            sd.check_output_settings(device=device_id, samplerate=44100, channels=2)
                            print(f"  ✅ Устройство доступно для вывода")
                        except Exception as e:
                            print(f"  ❌ Устройство НЕ доступно: {e}")
                            problematic_devices.append(name)
                    
                    # Проверка на проблемные паттерны
                    if is_bluetooth and max_input > 0:
                        print(f"  🔴 BLUETOOTH С МИКРОФОНОМ: может вызывать петли!")
                        problematic_devices.append(name)
                        
                except Exception as e:
                    print(f"  ❌ Ошибка анализа устройства: {e}")
                    continue
            
            # Итоговый отчет
            print(f"\n" + "="*70)
            print("📋 ИТОГОВЫЙ ОТЧЕТ ДИАГНОСТИКИ")
            print("="*70)
            
            if problematic_devices:
                print(f"\n🚨 ПРОБЛЕМНЫЕ УСТРОЙСТВА ({len(problematic_devices)}):")
                for device in problematic_devices:
                    print(f"  • {device}")
                print(f"\n💡 РЕКОМЕНДАЦИИ:")
                print(f"  1. Отключите микрофон на этих устройствах")
                print(f"  2. Используйте только как устройства ВЫВОДА")
                print(f"  3. Проверьте настройки Bluetooth профилей")
                print(f"  4. Рассмотрите использование только A2DP профиля")
            
            if loop_risk_devices:
                print(f"\n⚠️  РИСК АУДИО-ПЕТЕЛЬ ({len(loop_risk_devices)}):")
                for device in loop_risk_devices:
                    print(f"  • {device}")
            
            if bluetooth_devices:
                print(f"\n📱 BLUETOOTH УСТРОЙСТВА ({len(bluetooth_devices)}):")
                for device in bluetooth_devices:
                    print(f"  • {device}")
            
            print(f"\n🔧 РЕКОМЕНДАЦИИ ПО TRONSMART ELEMENT T6:")
            print(f"  1. Убедитесь, что используется только A2DP профиль")
            print(f"  2. Отключите HFP/HSP профили в настройках Bluetooth")
            print(f"  3. Проверьте что колонка не используется как микрофон")
            print(f"  4. Перезагрузите Bluetooth драйвер")
            
            print(f"\n✅ Диагностика завершена!")
            print("="*70)
            
            # Показываем результат в UI
            result_message = f"Диагностика завершена!\n\n"
            if problematic_devices:
                result_message += f"🚨 Найдено {len(problematic_devices)} проблемных устройств:\n"
                for device in problematic_devices[:3]:  # Показываем первые 3
                    result_message += f"• {device}\n"
                if len(problematic_devices) > 3:
                    result_message += f"... и еще {len(problematic_devices) - 3}\n"
                result_message += f"\n💡 Рекомендация: отключите микрофон на этих устройствах"
            else:
                result_message += f"✅ Проблемных устройств не найдено"
            
            self.show_message(result_message)
            
        except Exception as e:
            error_msg = f"❌ Ошибка диагностики: {e}"
            print(error_msg)
            self.show_message(error_msg)

    def _detect_audio_loop(self, indata, device_name: str) -> bool:
        """
        Обнаружение аудио-петли в режиме реального времени.
        Особенно важно для Bluetooth устройств как Tronsmart Element T6.
        """
        try:
            if not self.loop_protection_enabled:
                return False
            
            # Вычисляем RMS (среднеквадратичное значение) для анализа уровня сигнала
            rms = float(np.sqrt(np.mean(indata**2)))
            self.loop_detection_buffer.append(rms)
            
            # Нужно достаточно данных для анализа
            if len(self.loop_detection_buffer) < 50:
                return False
            
            # Конвертируем в список для анализа
            signal_levels = list(self.loop_detection_buffer)
            
            # Проверяем на экспоненциальный рост уровня сигнала (признак петли)
            if len(signal_levels) >= 10:
                recent_levels = signal_levels[-10:]
                early_levels = signal_levels[-20:-10] if len(signal_levels) >= 20 else signal_levels[:-10]
                
                if len(early_levels) > 0:
                    recent_avg = np.mean(recent_levels)
                    early_avg = np.mean(early_levels)
                    
                    # Если уровень сигнала резко возрос
                    if recent_avg > early_avg * 2.0 and recent_avg > 0.1:
                        print(f"⚠️  ОБНАРУЖЕНА ПОТЕНЦИАЛЬНАЯ ПЕТЛЯ: {device_name}")
                        print(f"   Уровень сигнала: {early_avg:.4f} → {recent_avg:.4f} (x{recent_avg/early_avg:.2f})")
                        
                        # Проверяем на повторяющийся паттерн
                        if self._check_repeating_pattern(signal_levels):
                            print(f"🚨 ПОДТВЕРЖДЕНА АУДИО-ПЕТЛЯ: {device_name}")
                            self.loop_protection_stats['loops_detected'] += 1
                            self.loop_protection_stats['last_loop_time'] = int(time.time())
                            
                            # Добавляем устройство в список проблемных
                            self.problematic_devices.add(device_name)
                            
                            return True
            
            return False
            
        except Exception as e:
            print(f"❌ Ошибка обнаружения петли: {e}")
            return False

    def _check_repeating_pattern(self, signal_levels) -> bool:
        """Проверяет наличие повторяющегося паттерна в сигнале."""
        try:
            if len(signal_levels) < 20:
                return False
            
            # Ищем корреляцию между разными частями сигнала
            half_size = len(signal_levels) // 2
            first_half = signal_levels[:half_size]
            second_half = signal_levels[half_size:half_size*2]
            
            if len(first_half) == len(second_half):
                correlation = np.corrcoef(first_half, second_half)[0, 1]
                if not np.isnan(correlation) and correlation > self.loop_detection_threshold:
                    print(f"🔍 Обнаружен повторяющийся паттерн (корреляция: {correlation:.3f})")
                    return True
            
            return False
            
        except Exception as e:
            print(f"❌ Ошибка анализа паттерна: {e}")
            return False

    def _prevent_audio_loop(self, device_name: str) -> bool:
        """
        Предотвращает аудио-петлю путем временного отключения устройства.
        """
        try:
            if not self.loop_prevention_enabled:
                return False
            
            print(f"🛡️  ПРЕДОТВРАЩЕНИЕ ПЕТЛИ: отключаю {device_name}")
            
            # Останавливаем поток для проблемного устройства
            if device_name in self.device_streams:
                input_stream, output_stream = self.device_streams[device_name]
                
                if output_stream:
                    try:
                        output_stream.stop()
                        output_stream.close()
                        print(f"✅ Поток {device_name} остановлен")
                    except Exception as e:
                        print(f"⚠️ Ошибка остановки потока: {e}")
                
                # Очищаем буфер устройства
                if device_name in self.buffers:
                    self.buffers[device_name].clear()
                    print(f"🧹 Буфер {device_name} очищен")
                
                # Удаляем из активных потоков
                del self.device_streams[device_name]
                
                self.loop_protection_stats['loops_prevented'] += 1
                
                # Показываем предупреждение пользователю
                self.show_message(f"⚠️ Обнаружена аудио-петля!\n\n"
                                f"Устройство '{device_name}' временно отключено для предотвращения петли.\n\n"
                                f"Рекомендации:\n"
                                f"• Проверьте настройки Bluetooth профилей\n"
                                f"• Отключите микрофон на этом устройстве\n"
                                f"• Используйте только A2DP профиль")
                
                return True
            
            return False
            
        except Exception as e:
            print(f"❌ Ошибка предотвращения петли: {e}")
            return False

    def _check_device_availability(self, device_id: int, device_name: str) -> bool:
        """Проверяет доступность аудио-устройства."""
        try:
            # Пробуем создать тестовый поток для проверки доступности
            test_stream = sd.OutputStream(
                device=device_id,
                samplerate=44100,
                channels=2,
                blocksize=256,
                dtype='float32'
            )
            test_stream.start()
            test_stream.stop()
            test_stream.close()
            return True
        except Exception as e:
            print(f"⚠️ Устройство '{device_name}' недоступно: {e}")
            return False

    def update_devices(self):
        """Update available audio devices with caching optimization."""
        try:
            # Проверяем кеш устройств
            current_time = time.time()
            # ИСПРАВЛЕНО: принудительное обновление при изменении устройств
            force_update = getattr(self, '_force_device_update', False)
            
            if (current_time - self.devices_cache_time) < self.cache_timeout and self.devices_cache and not force_update:
                # Используем кешированные данные
                filtered_sources = self.devices_cache.get('sources', [])
                filtered_targets = self.devices_cache.get('targets', [])
                print(f"📋 Используем кешированные устройства: {len(filtered_sources)} источников, {len(filtered_targets)} целей")
            else:
                # Обновляем кеш
                devices = sd.query_devices()
                filtered_sources = []
                filtered_targets = []
                seen_devices = set()

                # Кешируем host APIs для оптимизации
                host_apis_cache = {}
                try:
                    host_apis = sd.query_hostapis()
                    for i, api in enumerate(host_apis):
                        try:
                            # Безопасное извлечение имени API
                            if hasattr(api, 'get'):
                                host_apis_cache[i] = api.get('name', 'Unknown') # type: ignore
                            elif isinstance(api, dict):
                                host_apis_cache[i] = api['name'] if 'name' in api else 'Unknown'
                            else:
                                host_apis_cache[i] = str(api)
                        except Exception:
                            host_apis_cache[i] = 'Unknown'
                except Exception:
                    pass

                for device in devices:
                    try:
                        # Безопасное извлечение данных устройства
                        name = device.get('name', 'Unknown Device') # type: ignore
                        device_id = device.get('index', -1) # type: ignore
                        hostapi_id = device.get('hostapi', -1) # type: ignore
                        max_output = device.get('max_output_channels', 0) # type: ignore
                        max_input = device.get('max_input_channels', 0) # type: ignore

                        # Быстрое получение host API из кеша
                        host_api = host_apis_cache.get(hostapi_id, 'Unknown')

                        # Фильтруем только устройства с API MME
                        if host_api == "MME":
                            if max_output > 0 and max_input == 0:
                                # Исключаем системные виртуальные устройства
                                excluded_devices = [
                                    "Mapper", 
                                    "Переназначение звуковых устр",
                                    "Sound Mapper",
                                    "Primary Sound Driver",
                                    "Основной звуковой драйвер"
                                ]
                                
                                # Проверяем что устройство не является системным виртуальным
                                is_excluded = any(excluded in str(name) for excluded in excluded_devices)
                                
                                if is_excluded:
                                    print(f"🚫 Исключено системное устройство: {name}")
                                elif device_id not in seen_devices:
                                    if "Line 1 (Virtual Audio Cable)" in str(name):
                                        filtered_sources.append(str(name))
                                        print(f"📥 Добавлен источник: {name}")
                                    else:
                                        filtered_targets.append(str(name))
                                        print(f"📤 Добавлена цель: {name}")
                                    seen_devices.add(device_id)

                    except Exception as e:
                        print(f"⚠️ Ошибка обработки устройства: {e}")
                        continue

                # Обновляем кеш
                self.devices_cache = {
                    'sources': filtered_sources,
                    'targets': filtered_targets
                }
                self.devices_cache_time = current_time
                # Сбрасываем флаг принудительного обновления
                self._force_device_update = False
                print(f"🔄 Кеш устройств обновлен: {len(filtered_sources)} источников, {len(filtered_targets)} целей")
                print(f"📋 Источники: {filtered_sources}")
                print(f"🎯 Цели: {filtered_targets}")

            # Отложенное обновление UI для оптимизации
            self._schedule_ui_update(filtered_sources, filtered_targets)
            
        except Exception as e:
            print(f"❌ Ошибка обновления устройств: {e}")
            # Fallback к старому списку устройств
            if hasattr(self, 'devices_cache') and self.devices_cache:
                self._schedule_ui_update(
                    self.devices_cache.get('sources', []),
                    self.devices_cache.get('targets', [])
                )

    def _schedule_ui_update(self, sources, targets):
        """Отложенное обновление UI для оптимизации производительности."""
        def update_ui():
            try:
                # Обновляем только если есть изменения
                current_sources = []
                current_targets = []
                
                if self.source_combo.options:
                    current_sources = [opt.key if hasattr(opt, 'key') else opt.text for opt in self.source_combo.options]
                if self.target_combo.options:
                    current_targets = [opt.key if hasattr(opt, 'key') else opt.text for opt in self.target_combo.options]
                
                if current_sources != sources or current_targets != targets:
                    # ИСПРАВЛЕНО: безопасное обновление UI без прерывания потоков
                    old_source_value = self.source_combo.value
                    old_target_value = self.target_combo.value
                    
                    self.source_combo.options = [ft.dropdown.Option(device) for device in sources]
                    self.target_combo.options = [ft.dropdown.Option(device) for device in targets]
                    
                    # Восстанавливаем выбранные значения если они еще доступны
                    if old_source_value in sources:
                        self.source_combo.value = old_source_value
                    if old_target_value in targets:
                        self.target_combo.value = old_target_value
                    
                    self.page.update()
                    print(f"✅ UI безопасно обновлен: {len(sources)} источников, {len(targets)} целей")
                
            except Exception as e:
                print(f"⚠️ Ошибка обновления UI: {e}")
        
        # Запускаем обновление в отдельном потоке для неблокирующего выполнения
        import threading
        ui_thread = threading.Thread(target=update_ui)
        ui_thread.daemon = True
        ui_thread.start()

    def start_stream(self, device_name, source_device_id, sample_rate, blocksize):
        """Starts an output stream for a specific device."""
        target_device_id = self.get_device_id(device_name)
        if target_device_id is None:
            self.show_message(f"Устройство '{device_name}' не найдено")
            return None

        try:
            target_stream = sd.OutputStream(
                device=target_device_id, 
                samplerate=sample_rate, 
                channels=2,
                blocksize=blocksize,
                dtype=self.bit_depth,
                latency='low'  # Минимальная задержка
            )
            target_stream.start()
            self.buffers[device_name] = collections.deque(maxlen=sample_rate // blocksize * 3)  # 3 секунды буфера
            return target_stream
        except Exception as e:
            self.show_message(f"Ошибка запуска потока для {device_name}: {e}")
            return None

    @staticmethod
    def get_device_id(device_name):
        """Returns the device ID for a given device name."""
        try:
            devices = sd.query_devices()
            for sd_device in devices:
                name = sd_device.get('name', '')  # type: ignore
                if str(name) == device_name:
                    return sd_device.get('index', None)  # type: ignore
        except Exception as e:
            print(f"Ошибка получения ID устройства: {e}")
        return None

    def stop_streams(self):
        """Stops all active streams."""
        for device, streams in self.device_streams.items():
            input_stream, target_stream = streams
            try:
                if input_stream:
                    input_stream.stop()
                if target_stream:
                    target_stream.stop()
            except Exception as e:
                print(f"Ошибка остановки потока: {e}")
        self.device_streams.clear()

    def manage_capture(self, action="start"):
        if action == "start":
            if self.transmission_thread and self.transmission_thread.is_alive():
                self.show_message("Трансляция уже идет.")
                return
            source_device = self.source_combo.value
            if not source_device or not self.target_devices_list:
                self.show_message("Необходимо выбрать и источник, и хотя бы одно целевое устройство.")
                return
            self.restart_button.disabled = False
            self.transmission_thread = threading.Thread(target=self.manage_audio_stream,
                                                        args=(source_device, self.target_devices_list))
            self.transmission_thread.start()
        elif action == "stop":
            self.stop_event.set()
            if self.transmission_thread:
                self.transmission_thread.join()
            self.stop_streams()
            
            # Сбрасываем статистику при остановке
            self._reset_statistics()
            
            self.start_button.disabled = False
            self.stop_button.disabled = True
            self.restart_button.disabled = True
            self.toggle_device_controls(active=True)
            self.page.update()

    def start_capture(self):
        """Начинает запись аудио с предварительной валидацией."""
        # Проверяем наличие источника
        if not self.source_combo.value:
            self.show_message("❌ Выберите источник звука перед началом трансляции")
            return
        
        # Проверяем наличие целевых устройств
        if not self.target_devices_list:
            self.show_message("❌ Добавьте хотя бы одно целевое устройство")
            return
        
        # Проверяем доступность источника
        source_device_id = self.get_device_id(self.source_combo.value)
        if source_device_id is None:
            self.show_message("❌ Источник звука недоступен. Проверьте подключение устройства")
            return
        
        # Проверяем доступность целевых устройств
        unavailable_devices = []
        for device in self.target_devices_list:
            if self.get_device_id(device) is None:
                unavailable_devices.append(device)
        
        if unavailable_devices:
            self.show_message(f"❌ Недоступные устройства: {', '.join(unavailable_devices)}")
            return
        
        print(f"✅ Начинаю трансляцию: {self.source_combo.value} → {len(self.target_devices_list)} устройств")
        self.manage_capture(action="start")

    def restart_capture(self):
        self.stop_capture()
        time.sleep(1.5)
        self.start_capture()

    def stop_capture(self):
        self.manage_capture(action="stop")

    def adjust_value(self, device, control, delta, min_value, max_value, type="delay"):
        """Adjusts the delay or volume value within specified bounds."""
        if isinstance(control.value, str):
            current_value = int(control.value.strip()) if control.value.strip() else 0
        else:
            current_value = control.value

        new_value = max(min_value, min(max_value, current_value + delta))
        control.value = str(new_value)

        if type == "delay":
            self.update_delay(device, new_value)
        elif type == "volume":
            self.update_volume(device, new_value)

        slider_control = self.get_slider_control(device, type)
        if slider_control:
            slider_control.value = new_value

        self.page.update()
        return new_value

    def increment_delay(self, device, delay_input, delay_slider):
        self.adjust_value(device, delay_input, 10, 0, 3000, type="delay")

    def decrement_delay(self, device, delay_input, delay_slider):
        self.adjust_value(device, delay_input, -10, 0, 3000, type="delay")

    def increment_volume(self, device, volume_input, volume_slider):
        self.adjust_value(device, volume_input, 1, -10, 10, type="volume")

    def decrement_volume(self, device, volume_input, volume_slider):
        self.adjust_value(device, volume_input, -1, -10, 10, type="volume")

    def get_slider_control(self, device, type="delay"):
        """Возвращает соответствующий ползунок для устройства и типа."""
        if device in self.device_containers:
            if type == "delay":
                return self.device_containers[device].get("delay_slider")
            elif type == "volume":
                return self.device_containers[device].get("volume_slider")
        return None

    def update_value(self, device, input_control, slider_control=None, value_type="delay"):
        """Updates delay or volume based on the input control with validation."""
        try:
            # Получаем значение из управления
            if isinstance(input_control, int):
                new_value = input_control
            else:
                if input_control.value.strip() == "":
                    input_control.value = "0"
                    new_value = 0
                else:
                    new_value = float(input_control.value)

            # Валидация задержки
            if value_type == "delay":
                new_value = int(new_value)  # Задержка должна быть целым числом
                if new_value < 0:
                    self.show_message("❌ Задержка не может быть отрицательной")
                    new_value = 0
                elif new_value > 10000:
                    self.show_message("❌ Максимальная задержка: 10000 мс")
                    new_value = 10000
                
                self.delays[device] = new_value
                if not isinstance(input_control, int):
                    input_control.value = str(new_value)
                print(f"✅ Задержка для {device}: {new_value} мс")
            
            # Валидация громкости
            elif value_type == "volume":
                if new_value < -20:
                    self.show_message("❌ Минимальная громкость: -20 дБ")
                    new_value = -20
                elif new_value > 20:
                    self.show_message("❌ Максимальная громкость: +20 дБ")
                    new_value = 20
                
                self.volumes[device] = new_value
                if not isinstance(input_control, int):
                    input_control.value = str(new_value)
                print(f"✅ Громкость для {device}: {new_value:+.1f} дБ")
            
            # Обновляем ползунок
            if slider_control:
                slider_control.value = new_value

            self.page.update()
            self.save_settings()
            
        except ValueError:
            # Восстанавливаем предыдущее значение при ошибке
            if value_type == "delay":
                old_value = self.delays.get(device, 0)
                self.show_message("❌ Задержка должна быть целым числом (0-10000)")
            else:
                old_value = self.volumes.get(device, 0)
                self.show_message("❌ Громкость должна быть числом (-20 до +20)")
            
            if not isinstance(input_control, int):
                input_control.value = str(old_value)
            self.page.update()
            
        except Exception as e:
            print(f"⚠️ Ошибка валидации: {e}")
            self.show_message(f"❌ Ошибка валидации значения: {e}")

    def update_delay(self, device, delay_input, delay_slider=None):
        self.update_value(device, delay_input, delay_slider, value_type="delay")

    def update_delay_from_slider(self, device, delay_slider, delay_input=None):
        """Обновляет задержку при перемещении ползунка."""
        new_delay_ms = int(delay_slider.value)
        self.delays[device] = new_delay_ms
        if delay_input:
            delay_input.value = str(new_delay_ms)
        self.page.update()

    def update_volume(self, device, volume_input, volume_slider=None):
        self.update_value(device, volume_input, volume_slider, value_type="volume")

    def update_volume_from_slider(self, device, volume_slider, volume_input=None):
        """Обновляет громкость при перемещении ползунка."""
        new_volume_db = int(volume_slider.value)
        self.volumes[device] = new_volume_db
        if volume_input:
            volume_input.value = str(new_volume_db)
        self.page.update()

    def manage_audio_stream(self, source_device_name, target_devices=None, new_device=None, sample_rate=None,
                            blocksize=None):
        """Manages the audio stream."""
        try:
            # Используем настройки из класса если не переданы параметры
            if sample_rate is None:
                sample_rate = self.sample_rate
            if blocksize is None:
                blocksize = self.blocksize
                
            source_device_id = self.get_device_id(source_device_name)
            if source_device_id is None:
                self.show_message(f"Источник '{source_device_name}' не найден")
                return

            if target_devices is None:
                target_devices = []

            if new_device:
                target_devices = [new_device]
                
            # Обновляем статистику при запуске
            self.stream_stats['start_time'] = time.time()
            self.stream_stats['total_frames'] = 0
            self.stream_stats['errors_count'] = 0
            self.stream_stats['total_callbacks'] = 0
            self.stream_stats['data_processed_mb'] = 0.0
            self.stream_stats['last_callback_time'] = 0
            self.stream_stats['callback_intervals'].clear()
            print(f"📊 Статистика сброшена, запуск для {len(target_devices)} устройств")

            target_streams = []
            for target_device_name in target_devices:
                target_stream = self.start_stream(target_device_name, source_device_id, sample_rate, blocksize)
                if target_stream:
                    if new_device:
                        self.device_streams[target_device_name] = (None, target_stream)
                    target_streams.append((target_stream, target_device_name))

            def callback(indata, frames, time, status):
                """Улучшенная callback функция со статистикой и защитой от петель."""
                import time as time_module
                current_callback_time = time_module.time()
                
                if status:
                    print(f"🔊 Статус ошибки: {status}")
                    self.stream_stats['errors_count'] += 1
                
                # КРИТИЧЕСКИ ВАЖНО: Обнаружение аудио-петель для Bluetooth устройств
                try:
                    # Проверяем на аудио-петли (особенно для Tronsmart Element T6)
                    if self._detect_audio_loop(indata, source_device_name):
                        print(f"🚨 ОБНАРУЖЕНА АУДИО-ПЕТЛЯ: {source_device_name}")
                        # Немедленно прекращаем обработку для предотвращения петли
                        return
                except Exception as e:
                    print(f"⚠️ Ошибка обнаружения петли: {e}")
                    # Продолжаем работу даже если обнаружение петли не сработало
                
                # ИСПРАВЛЕНО: правильная статистика
                self.stream_stats['total_frames'] += frames
                self.stream_stats['total_callbacks'] += 1
                
                # Измеряем объем обработанных данных (frames × каналы × байты на sample)
                data_size_bytes = frames * 2 * 4  # 2 канала × 4 байта (float32)
                self.stream_stats['data_processed_mb'] += data_size_bytes / (1024 * 1024)
                
                # Измеряем стабильность интервалов между callback'ами
                if self.stream_stats['last_callback_time'] > 0:
                    interval = current_callback_time - self.stream_stats['last_callback_time']
                    self.stream_stats['callback_intervals'].append(interval)
                self.stream_stats['last_callback_time'] = current_callback_time
                
                # Управление памятью - периодическая очистка
                self.memory_cleanup_counter += 1
                if self.memory_cleanup_counter >= self.memory_cleanup_interval:
                    try:
                        # Очищаем переполненные буферы
                        for device_name in list(self.buffers.keys()):
                            buffer = self.buffers.get(device_name)
                            if buffer and len(buffer) > 1000:  # Если буфер слишком большой
                                buffer.clear()
                        self.memory_cleanup_counter = 0
                    except Exception as e:
                        print(f"⚠️ Ошибка очистки памяти в callback: {e}")
                
                # Антиалиасинг фильтр для высоких частот дискретизации
                if sample_rate > 48000:
                    # Применяем сглаживание для высоких частот
                    filtered_data = indata.copy()
                    if len(filtered_data) > 1:
                        filtered_data[1:] = filtered_data[1:] * 0.9 + filtered_data[:-1] * 0.1
                else:
                    filtered_data = indata.copy()
                
                streams = target_streams if not new_device else [(self.device_streams[new_device][1], new_device)]
                for target_stream, target_device_name in streams:
                    try:
                        # ИСПРАВЛЕНИЕ: Проверка маршрутизации перед обработкой звука
                        if not self.should_route_to_device(target_device_name):
                            # Если маршрутизация запрещена, пропускаем этот поток
                            continue
                        
                        # Получаем задержку в миллисекундах
                        delay_ms = self.delays.get(target_device_name, 0)
                        
                        # Преобразуем в секунды (обычно мс → сек)
                        delay_s = delay_ms / 1000.0
                        
                        # ДОПОЛНИТЕЛЬНАЯ ЗАЩИТА: если задержки все еще слишком большие
                        if self.delay_debug_mode:
                            delay_s = delay_s / 1000.0  # Еще раз делим на 1000
                            print(f"🐛 DEBUG: дополнительное деление для {target_device_name}: {delay_ms}мс → {delay_s}с")
                        
                        volume_db = self.volumes.get(target_device_name, 0)
                        volume_factor = 10 ** (volume_db / 20.0)

                        buffer = self.buffers[target_device_name]
                        
                        # ИСПРАВЛЕНИЕ: правильно считаем количество порций для задержки
                        required_frames = int(sample_rate * delay_s)
                        required_chunks = max(1, required_frames // blocksize)  # Порции, не фреймы!
                        
                        # Диагностика (только при первом callback для каждого устройства)
                        if target_device_name not in self._delay_debug_printed:
                            real_delay_ms = (required_chunks * blocksize / sample_rate) * 1000
                            print(f"📊 {target_device_name}: установлено {delay_ms}мс → {required_chunks} порций → реально {real_delay_ms:.1f}мс")
                            
                            # Автоматическое включение debug режима если задержка слишком большая
                            if delay_ms > 0 and real_delay_ms > delay_ms * 10:  # Если реальная в 10 раз больше
                                print(f"⚠️ ОБНАРУЖЕНА ПРОБЛЕМА: задержка в 10 раз больше ожидаемой!")
                                print(f"💡 Включаю debug режим с дополнительным делением на 1000...")
                                self.delay_debug_mode = True
                                
                            self._delay_debug_printed.add(target_device_name)
                        
                        # Защита от переполнения буфера
                        if len(buffer) > required_chunks * 3:  # Если буфер в 3 раза больше нужного
                            buffer.clear()
                            print(f"🧹 Буфер {target_device_name} очищен (переполнение)")

                        # Применяем громкость с мягким ограничением
                        modified_audio = filtered_data * volume_factor
                        
                        # Мягкое ограничение для предотвращения клиппинга
                        if volume_factor > 1.0:
                            modified_audio = np.tanh(modified_audio * 0.9) * 1.1
                        
                        buffer.append(modified_audio)

                        # ИСПРАВЛЕНИЕ: правильное воспроизведение с задержкой
                        while len(buffer) > required_chunks:
                            out_data = buffer.popleft()
                            target_stream.write(out_data)
                            
                    except Exception as e:
                        print(f"⚠️  Ошибка обработки {target_device_name}: {e}")
                        self.stream_stats['errors_count'] += 1
                        continue

            if not new_device:
                with sd.InputStream(device=source_device_id, channels=2, callback=callback,
                                    samplerate=sample_rate, blocksize=blocksize):
                    self.stop_event.clear()
                    self.start_button.disabled = True
                    self.stop_button.disabled = False
                    self.page.update()
                    while not self.stop_event.is_set():
                        sd.sleep(100)
            else:
                new_stream = sd.InputStream(device=source_device_id, channels=2, callback=callback,
                                            samplerate=sample_rate, blocksize=blocksize)
                new_stream.start()
                self.device_streams[new_device] = (new_stream, self.device_streams[new_device][1])
                self.page.update()

        except Exception as e:
            self.show_message(f"Ошибка в аудиопотоке: {e}")
        finally:
            if not new_device:
                self.stop_streams()
                self.page.update()

    def add_device(self, device):
        """Добавляет новое устройство в список."""
        source_device = self.source_combo.value

        if not source_device:
            self.show_message("Необходимо выбрать источник звука перед добавлением целевого устройства.")
            return

        if device and device not in self.target_devices_list:
            self.target_devices_list.append(device)
            self.add_device_to_ui(device)

            if self.transmission_thread and self.transmission_thread.is_alive():
                self.manage_audio_stream(source_device, new_device=device)

    def add_device_to_ui(self, device):
        """Add the device UI elements for the newly added device."""
        device_settings = self.device_settings.get(device, {})
        delay_ms = device_settings.get('delay', 0)
        volume_db = device_settings.get('volume', 0)

        self.delays[device] = delay_ms
        self.volumes[device] = volume_db
        self.buffers[device] = collections.deque()

        # UI элементы
        divider = ft.Divider(height=10, thickness=2, color="gray")
        
        delay_slider = ft.Slider(
            value=delay_ms,
            min=0,
            max=3000,
            divisions=100,
            label="{value} мс",
            on_change=lambda e, d=device: self.update_delay_from_slider(d, e.control, delay_input),
            expand=True
        )

        delay_input = ft.TextField(
            label="Задержка (мс)",
            value=str(delay_ms),
            width=125,
            text_align=ft.TextAlign.CENTER,
            on_change=lambda e, d=device: self.update_delay(d, e.control, delay_slider),
            on_focus=lambda e: self.clear_default_value(e),
            on_blur=lambda e: self.restore_default_value(e),
            border_radius=10
        )

        volume_slider = ft.Slider(
            value=volume_db,
            min=-10,
            max=10,
            divisions=20,
            label="{value} дБ",
            on_change=lambda e, d=device: self.update_volume_from_slider(d, e.control, volume_input),
            expand=True
        )

        volume_input = ft.TextField(
            label="Громкость (дБ)",
            value=str(volume_db),
            width=125,
            text_align=ft.TextAlign.CENTER,
            on_change=lambda e, d=device: self.update_volume(d, e.control, volume_slider),
            on_focus=lambda e: self.clear_default_value(e),
            on_blur=lambda e: self.restore_default_value(e),
            border_radius=10
        )

        # Кнопки управления
        increment_volume_button = ft.IconButton(
            icon="add", 
            on_click=lambda e, d=device: self.increment_volume(d, volume_input, volume_slider),
            tooltip="Увеличить громкость"
        )
        decrement_volume_button = ft.IconButton(
            icon="remove", 
            on_click=lambda e, d=device: self.decrement_volume(d, volume_input, volume_slider),
            tooltip="Уменьшить громкость"
        )

        increment_button = ft.IconButton(
            icon="add", 
            on_click=lambda e, d=device: self.increment_delay(d, delay_input, delay_slider),
            tooltip="Увеличить задержку"
        )
        decrement_button = ft.IconButton(
            icon="remove", 
            on_click=lambda e, d=device: self.decrement_delay(d, delay_input, delay_slider),
            tooltip="Уменьшить задержку"
        )

        remove_button = ft.IconButton(
            icon="delete", 
            on_click=lambda e, d=device: self.remove_device(d),
            tooltip="Удалить устройство"
        )

        # Создаем контейнер устройства
        device_container = ft.Container(
            content=ft.Column(
                [
                    ft.Row([
                        ft.Text(f"🔊 {device}", size=16, weight=ft.FontWeight.BOLD),
                        remove_button
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.Row([
                        decrement_button,
                        delay_input,
                        increment_button
                    ], alignment=ft.MainAxisAlignment.CENTER),
                    delay_slider,
                    divider,
                    ft.Row([
                        decrement_volume_button,
                        volume_input,
                        increment_volume_button
                    ], alignment=ft.MainAxisAlignment.CENTER),
                    volume_slider,
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10
            ),
            border=ft.border.all(2, "blue"),
            border_radius=15,
            padding=15,
            margin=5,
            width=350,
            bgcolor="surface",
            shadow=ft.BoxShadow(
                spread_radius=1,
                blur_radius=5,
                color="black12",
                offset=ft.Offset(0, 2)
            )
        )
        
        self.selected_devices_list.controls.append(device_container)
        
        self.device_containers[device] = {
            "delay_slider": delay_slider,
            "volume_slider": volume_slider,
            "container": device_container
        }
        
        # Безопасное обновление UI с проверками
        try:
            if hasattr(self, 'page') and self.page:
                self.page.update()
        except Exception as e:
            print(f"⚠️ Ошибка обновления UI в add_device_to_ui: {e}")
        
        self.update_panel_visibility()
        
        # Обновляем audio_router с проверкой
        # if hasattr(self, 'audio_router') and self.audio_router:
        #     self.audio_router.update_devices(self.target_devices_list)  # Отключено

    def remove_device(self, device):
        """Removes a device from the list and stops its stream."""
        if self.transmission_thread and self.transmission_thread.is_alive():
            self.show_message_with_stop_button("Невозможно выполнить пока включен поток.")
            return

        if device in self.device_streams:
            _, target_stream = self.device_streams[device]
            try:
                target_stream.stop()
            except:
                pass
            del self.device_streams[device]

        if device in self.target_devices_list:
            index = self.target_devices_list.index(device)
            self.target_devices_list.pop(index)
            self.selected_devices_list.controls.pop(index)

            self.device_settings[device] = {
                'delay': self.delays.get(device, 0),
                'volume': self.volumes.get(device, 0)
            }

            # Очистка данных устройства
            for key in ['delays', 'volumes', 'buffers', 'device_containers']:
                device_dict = getattr(self, key, {})
                if device in device_dict:
                    del device_dict[device]

            self.update_panel_visibility()
            
            # Безопасное обновление UI
            try:
                if hasattr(self, 'page') and self.page:
                    self.page.update()
            except Exception as e:
                print(f"⚠️ Ошибка обновления UI в remove_device: {e}")

    def update_panel_visibility(self):
        """Updates the visibility of the devices panel and the clear button."""
        try:
            has_devices = len(self.target_devices_list) > 0
            
            if hasattr(self, 'devices_panel'):
                self.devices_panel.visible = has_devices
            if hasattr(self, 'clear_button'):
                self.clear_button.visible = has_devices
                
            # Безопасное обновление с проверкой
            if hasattr(self, 'page') and self.page:
                try:
                    self.page.update()
                except Exception as update_error:
                    print(f"⚠️ Ошибка обновления UI в update_panel_visibility: {update_error}")
                    
        except Exception as e:
            print(f"❌ Ошибка в update_panel_visibility: {e}")

    def toggle_device_controls(self, active: bool):
        """Toggles the activity of the remove buttons and the clear button."""
        try:
            if hasattr(self, 'clear_button'):
                self.clear_button.disabled = not active
                
            # Безопасное обновление UI
            if hasattr(self, 'page') and self.page:
                try:
                    self.page.update()
                except Exception as update_error:
                    print(f"⚠️ Ошибка обновления UI в toggle_device_controls: {update_error}")
        except Exception as e:
            print(f"❌ Ошибка переключения контролов: {e}")

    def clear_default_value(self, event):
        """Clears the default value of a text field if it is zero."""
        try:
            if event.control.value == "0":
                event.control.value = ""
                if hasattr(self, 'page') and self.page:
                    self.page.update()
        except Exception as e:
            print(f"⚠️ Ошибка в clear_default_value: {e}")

    def restore_default_value(self, event):
        """Restores the default value of a text field if it is empty."""
        try:
            if event.control.value.strip() == "":
                event.control.value = "0"
                if hasattr(self, 'page') and self.page:
                    self.page.update()
        except Exception as e:
            print(f"⚠️ Ошибка в restore_default_value: {e}")

    def window_event_handler(self, e):
        """Handles window events, like closing the app."""
        if e.data == "close":
            self.close_event()

    def close_event(self):
        """Handles the application close event, ensuring a clean shutdown."""
        self.save_settings()
        self.stop_event.set()
        
        # Останавливаем таймер обновления статуса
        if hasattr(self, 'status_timer'):
            try:
                self.status_timer.cancel()
                print("🔕 Таймер статуса остановлен")
            except Exception as e:
                print(f"⚠️ Ошибка остановки таймера: {e}")
        

        
        # Выполняем очистку памяти
        self._cleanup_memory()
        
        if self.transmission_thread and self.transmission_thread.is_alive():
            self.show_message("Остановка трансляции перед закрытием программы, пожалуйста, подождите...")
            time.sleep(0.5)
            self.transmission_thread.join()
        else:
            self.show_message("Программа закрывается, пожалуйста, подождите...")
            time.sleep(0.5)

        self.page.window.destroy()
        print("✅ Программа завершена корректно")

    def _cleanup_memory(self):
        """Очистка памяти для предотвращения утечек."""
        try:
            # Очищаем буферы
            for device in list(self.buffers.keys()):
                try:
                    self.buffers[device].clear()
                except Exception:
                    pass
            
            # Очищаем кеши
            if hasattr(self, 'devices_cache'):
                self.devices_cache.clear()
            
            # Сброс счетчиков
            self.memory_cleanup_counter = 0
            self.stream_stats['total_frames'] = 0
            
            print("🧹 Очистка памяти выполнена")
        except Exception as e:
            print(f"⚠️ Ошибка очистки памяти: {e}")

    def _attempt_recovery(self, error_msg: str):
        """Автоматическое восстановление после ошибок."""
        current_time = time.time()
        
        # Проверяем не слишком ли часто происходят ошибки
        if current_time - self.last_error_time < self.error_recovery_delay:
            self.recovery_attempts += 1
        else:
            self.recovery_attempts = 1
        
        self.last_error_time = current_time
        
        if self.recovery_attempts <= self.max_recovery_attempts:
            print(f"🔄 Попытка восстановления #{self.recovery_attempts}: {error_msg}")
            
            try:
                # Очищаем буферы
                for device in list(self.buffers.keys()):
                    if device in self.buffers:
                        self.buffers[device].clear()
                
                # Обновляем список устройств
                self.update_devices()
                
                # Сбрасываем статистику ошибок если восстановление успешно
                if self.recovery_attempts == 1:
                    self.stream_stats['errors_count'] = 0
                
                print(f"✅ Восстановление #{self.recovery_attempts} успешно")
                return True
                
            except Exception as e:
                print(f"❌ Восстановление #{self.recovery_attempts} неудачно: {e}")
                return False
        else:
            print(f"❌ Превышено максимальное количество попыток восстановления ({self.max_recovery_attempts})")
            self.show_message(f"❌ Критическая ошибка: {error_msg}. Перезапустите приложение.")
            return False

    def clear_devices(self):
        """Clears the list of devices."""
        if self.transmission_thread and self.transmission_thread.is_alive():
            self.show_message_with_stop_button("Невозможно выполнить пока включен поток.")
            return

        self.stop_capture()
        self.target_devices_list.clear()
        self.selected_devices_list.controls.clear()
        self.buffers.clear()
        self.delays.clear()
        self.volumes.clear()
        self.device_containers.clear()
        self.update_panel_visibility()
        
        # Безопасное обновление UI
        try:
            if hasattr(self, 'page') and self.page:
                self.page.update()
        except Exception as e:
            print(f"⚠️ Ошибка обновления UI в clear_devices: {e}")

    def show_message(self, message: str):
        """Показывает сообщение в центре окна с безопасным обновлением."""
        try:
            # Проверяем что page доступна
            if not hasattr(self, 'page') or not self.page:
                print(f"⚠️ Сообщение (page недоступна): {message}")
                return
                
            dialog = ft.AlertDialog(
                title=ft.Text(message),
                actions=[
                    ft.TextButton("OK", on_click=lambda e: self.close_dialog(dialog))
                ]
            )
            
            # Безопасное добавление в overlay
            if hasattr(self.page, 'overlay'):
                self.page.overlay.append(dialog)
                dialog.open = True
                
                # Безопасное обновление с проверкой
                try:
                    self.page.update()
                except Exception as update_error:
                    print(f"⚠️ Ошибка обновления UI в show_message: {update_error}")
                    # Альтернативный способ - просто логируем
                    print(f"📝 Сообщение: {message}")
            else:
                print(f"📝 Сообщение (overlay недоступен): {message}")
                
        except Exception as e:
            print(f"❌ Критическая ошибка show_message: {e}")
            print(f"📝 Исходное сообщение: {message}")

    def close_dialog(self, dialog):
        """Закрывает диалог."""
        dialog.open = False
        self.page.update()

    def show_message_with_stop_button(self, message: str):
        """Shows a message dialog with the stop button."""
        dialog = ft.AlertDialog(
            title=ft.Text(message),
            actions=[
                ft.ElevatedButton(
                    text="Остановить",
                    on_click=lambda e: self.stop_stream_and_close_dialog(e, dialog),
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=10)
                    )
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.CENTER,
        )
        self.page.overlay.append(dialog)
        dialog.open = True
        self.page.update()

    def stop_stream_and_close_dialog(self, e, dialog):
        """Stops the stream and closes the dialog."""
        self.stop_capture()
        dialog.open = False
        self.page.update()

    def on_advanced_settings_click(self):
        """Заглушка для расширенных настроек."""
        print("🔧 Запрос на открытие расширенных настроек...")
        
        #"""Открываем интерфейс для настройки маршрутизации аудиопотоков."""
        # ИСПРАВЛЕНИЕ: Проверки состояния перед открытием
        #try:
            # 1. Проверка активных потоков
            #if self.transmission_thread and self.transmission_thread.is_alive():
               # active_streams = len([s for s in self.device_streams.values() if s])
                #if active_streams > 0:
                    #self.show_message(
                        #"⚠️ Расширенные настройки недоступны!\n\n"
                        #f"Активно {active_streams} аудиопотоков.\n"
                        #"Сначала остановите трансляцию, затем откройте расширенные настройки."
                    #)
                    #print("⚠️ Расширенные настройки заблокированы - активна трансляция")
                    #return
            
            # 2. Проверка источника звука
            #if not self.source_combo.value:
                #self.show_message(
                    #"⚠️ Не выбран источник звука!\n\n"
                    #"Выберите источник звука перед настройкой маршрутизации."
                #)
                #print("⚠️ Расширенные настройки заблокированы - нет источника звука")
                #return
            
            # 3. Проверка целевых устройств
            #if not self.target_devices_
        # Показываем сообщение что функция пока не работает
        self.show_message(
            "⚠️ Расширенные настройки\n\n"
            "Функция пока не работает.\n"
            "Находится в разработке."
        )
        print("⚠️ Расширенные настройки - функция отключена (заглушка)")

    def toggle_language(self, _):
        """Переключение языка."""
        self.language = 'en' if self.language == 'ru' else 'ru'
        self.settings["language"] = self.language
        self.settings_manager.save(self.settings)
        self.update_texts()

    def get_translation(self, text):
        """Возвращает перевод текста в зависимости от текущего языка."""
        translations = {
            "ru": {
                "Источник звука": "Источник звука",
                "Целевые устройства": "Целевые устройства",
                "Расширенные настройки": "Расширенные настройки",
                "День": "День",
                "Ночь": "Ночь",
                "Перезапустить": "Перезапустить",
                "Запустить": "Запустить",
                "Остановить": "Остановить",
                "Добавить": "Добавить",
                "Очистить список": "Очистить список",
                "Задержка (мс)": "Задержка (мс)",
                "Громкость (дБ)": "Громкость (дБ)",
            },
            "en": {
                "Источник звука": "Sound Source",
                "Целевые устройства": "Target Devices",
                "Расширенные настройки": "Advanced Settings",
                "День": "Day",
                "Ночь": "Night",
                "Перезапустить": "Restart",
                "Запустить": "Start",
                "Остановить": "Stop",
                "Добавить": "Add",
                "Очистить список": "Clear List",
                "Задержка (мс)": "Delay (ms)",
                "Громкость (дБ)": "Volume (dB)",
            }
        }
        return translations[self.language].get(text, text)

    def update_texts(self):
        """Обновление всех текстов на текущем языке."""
        self.source_combo.label = self.get_translation("Источник звука")
        self.target_combo.label = self.get_translation("Целевые устройства")
        self.advanced_settings_button.text = self.get_translation("Расширенные настройки")
        self.theme_toggle_button.text = self.get_translation("День") if not self.is_dark_mode else self.get_translation("Ночь")
        
        self.restart_button.text = self.get_translation("Перезапустить")
        self.start_button.text = self.get_translation("Запустить")
        self.stop_button.text = self.get_translation("Остановить")
        self.add_button.text = self.get_translation("Добавить")
        self.clear_button.text = self.get_translation("Очистить список")
        self.language_toggle_button.text = "Рус" if self.language == 'ru' else "Eng"

        self.page.update()


def main(page: ft.Page):
    app = AudioForwarderApp(page)
    page.update()


if __name__ == "__main__":
    ft.app(target=main)
