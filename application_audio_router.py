import psutil
import asyncio
import os
import json
import flet as ft
import pygetwindow as gw
import ctypes
import threading
import time
import win32gui


# Функция для получения имени процесса по hwnd
def get_process_name(hwnd):
    try:
        # Получаем PID процесса через hwnd
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = psutil.Process(pid.value)
        return process.name()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return "Unknown"


class ApplicationAudioRouter:
    def __init__(self, target_devices_list, app_instance):
        self.target_devices_list = target_devices_list
        self.device_streams = {}  # Инициализация словаря для хранения активных потоков устройств
        self.device_settings = {}  # Другие необходимые инициализации
        self.devices = target_devices_list  # Список устройств, переданный из главного приложения
        self.app = app_instance  # Экземпляр AudioForwarderApp
        self.applications = {}  # Инициализация словаря приложений
        
        # ИСПРАВЛЕНИЕ: Добавляем контроль жизненного цикла
        self._stop_event = threading.Event()
        self._monitoring_task = None
        self._dialog_open = False
        self._last_update_time = 0
        self._update_interval = 3.0  # Уменьшаем частоту обновлений до 3 секунд
        
        # ИСПРАВЛЕНИЕ: Правильное управление источником звука
        self.source_device_name = None
        self.source_device_id = None
        
        # ИСПРАВЛЕНИЕ: Система обработки ошибок
        self.error_counts = {
            'monitoring': 0,
            'interface': 0,
            'devices': 0,
            'settings': 0
        }
        self.last_error_time = 0
        self.max_errors_per_category = 5
        self.error_reset_interval = 300  # 5 минут
        
        self.load_settings()  # Загружаем настройки при инициализации

    def load_settings(self):
        """Загрузка настроек из файла."""
        if os.path.exists('audio_router_settings.json'):
            try:
                with open('audio_router_settings.json', 'r', encoding='utf-8') as f:
                    self.device_settings = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                self.device_settings = {}

    def save_settings(self):
        """Сохранение настроек в файл."""
        try:
            with open('audio_router_settings.json', 'w', encoding='utf-8') as f:
                json.dump(self.device_settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка сохранения настроек: {e}")

    def select_devices_for_app(self, app_name, selected_devices):
        """Сохраняет выбор устройств вывода звука для приложения."""
        self.device_settings[app_name] = selected_devices
        self.save_settings()

    def stop_monitoring(self):
        """Остановка мониторинга и очистка ресурсов."""
        print("🔴 Остановка мониторинга ApplicationAudioRouter...")
        self._stop_event.set()
        self._dialog_open = False
        
        # Останавливаем все потоки устройств
        for device_name, streams in list(self.device_streams.items()):
            try:
                if streams and len(streams) > 1:
                    _, target_stream = streams
                    if hasattr(target_stream, 'active') and target_stream.active:
                        target_stream.stop()
                    if hasattr(target_stream, 'close'):
                        target_stream.close()
                    print(f"✅ Поток для {device_name} остановлен")
            except Exception as e:
                print(f"⚠️ Ошибка остановки потока {device_name}: {e}")
        
        self.device_streams.clear()
        self.applications.clear()
        print("✅ ApplicationAudioRouter остановлен")

    async def update_applications(self):
        """Обновляет список запущенных приложений с контролем ресурсов."""
        print("🔄 Запуск мониторинга приложений...")
        cycle_count = 0
        
        while not self._stop_event.wait(0.1):  # Неблокирующая проверка
            try:
                # Проверка состояния системы
                is_valid, error_msg = self._validate_state()
                if not is_valid:
                    print(f"⚠️ Мониторинг остановлен: {error_msg}")
                    break
                
                cycle_count += 1
                current_time = time.time()
                
                # Обновляем только если прошло достаточно времени
                if current_time - self._last_update_time < self._update_interval:
                    await asyncio.sleep(0.5)
                    continue
                
                self._last_update_time = current_time
                
                # Получаем приложения только если диалог открыт
                if not self._dialog_open:
                    await asyncio.sleep(1.0)
                    continue
                
                current_apps = {}
                
                # ОПТИМИЗАЦИЯ: Фильтруем только нужные окна
                try:
                    visible_windows = gw.getWindowsWithTitle('')
                    audio_relevant_processes = {
                        'chrome.exe', 'firefox.exe', 'msedge.exe', 'opera.exe',
                        'spotify.exe', 'vlc.exe', 'wmplayer.exe', 'winamp.exe',
                        'foobar2000.exe', 'aimp.exe', 'potplayer.exe', 'mpc-hc.exe',
                        'steam.exe', 'discord.exe', 'telegram.exe', 'zoom.exe',
                        'teams.exe', 'skype.exe', 'obs64.exe', 'streamlabs.exe'
                    }
                    
                    for win in visible_windows:
                        if (win.visible and win.title.strip() != '' and 
                            len(win.title) > 3 and win.title != 'Program Manager'):
                            
                            try:
                                hwnd = win._hWnd
                                app_name = get_process_name(hwnd)
                                
                                # Фильтруем только аудио-релевантные приложения или добавленные пользователем
                                if (app_name.lower() in audio_relevant_processes or
                                    win.title in self.device_settings or
                                    any(keyword in win.title.lower() for keyword in 
                                        ['музыка', 'music', 'audio', 'звук', 'video', 'видео', 'player'])):
                                    
                                    current_apps[hwnd] = {
                                        "title": win.title,
                                        "app_name": app_name
                                    }
                            except Exception as e:
                                # Мелкие ошибки обработки окон не критичны
                                continue
                                
                except Exception as e:
                    # Критическая ошибка получения списка окон
                    self._handle_error('monitoring', e, "Ошибка получения списка окон", show_user=False)
                    await asyncio.sleep(2.0)
                    continue

                # Обновляем только если есть изменения
                if current_apps != self.applications:
                    self.applications = current_apps
                    print(f"📱 Обновлен список приложений: {len(current_apps)} элементов")
                
                # Логируем статистику каждые 20 циклов
                if cycle_count % 20 == 0:
                    print(f"📊 Мониторинг: цикл {cycle_count}, приложений {len(current_apps)}")
                
                await asyncio.sleep(1.0)  # Пауза между итерациями
                
            except Exception as e:
                # Обработка критических ошибок в цикле мониторинга
                error_id = self._handle_error('monitoring', e, f"Цикл мониторинга #{cycle_count}", show_user=False)
                
                # Если слишком много ошибок, останавливаем мониторинг
                if self.error_counts['monitoring'] > self.max_errors_per_category:
                    print(f"🚨 Мониторинг остановлен из-за критических ошибок")
                    break
                
                await asyncio.sleep(3.0)  # Увеличенная пауза при ошибке
        
        print("✅ Мониторинг приложений остановлен")

    async def show_interface(self, page: ft.Page):
        """Отображает интерфейс для управления аудиомаршрутизацией."""
        print("🖥️ Открытие интерфейса расширенных настроек...")
        
        try:
            # Проверка состояния перед открытием интерфейса
            is_valid, error_msg = self._validate_state()
            if not is_valid:
                self._handle_error('interface', Exception(error_msg), "Проверка состояния интерфейса", show_user=True)
                return
            
            self._dialog_open = True
            
            # Создаем контейнер для содержимого диалога
            app_list = ft.Column(
                scroll=ft.ScrollMode.AUTO,
                expand=True,
                spacing=10
            )
            
            # УЛУЧШЕНИЕ: Адаптивный размер и лучший дизайн
            dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text("🎵 Маршрутизация аудио по приложениям", size=18, weight=ft.FontWeight.BOLD),
                content=ft.Container(
                    width=800,  # Уменьшенная ширина
                    height=400,  # Уменьшенная высота
                    padding=15,
                    content=ft.Column([
                        ft.Text("Выберите устройства вывода для каждого приложения:", size=14),
                        ft.Divider(height=1),
                        ft.Container(
                            content=app_list,
                            expand=True
                        )
                    ])
                ),
                actions=[
                    ft.Row([
                        ft.ElevatedButton(
                            "🔄 Обновить список", 
                            on_click=lambda e: self.force_refresh_apps(app_list, page),
                            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10))
                        ),
                        ft.ElevatedButton(
                            "❌ Закрыть", 
                            on_click=lambda e: self.close_dialog(page, dialog),
                            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10))
                        )
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
                ],
                actions_alignment=ft.MainAxisAlignment.SPACE_BETWEEN
            )

            # Первоначальное заполнение списка приложений
            await self.populate_app_list(app_list)

            if not page or not hasattr(page, 'overlay'):
                self._handle_error('interface', Exception("Недоступна страница"), "Проверка page", show_user=True)
                return

            page.overlay.append(dialog)
            dialog.open = True
            page.update()

            # ИСПРАВЛЕНИЕ: Контролируемый цикл обновления вместо бесконечного
            last_apps_count = len(self.applications)
            update_counter = 0
            
            while dialog.open and not self._stop_event.is_set():
                try:
                    await asyncio.sleep(2.0)  # Увеличенный интервал обновления
                    
                    # Обновляем только если есть изменения
                    current_apps_count = len(self.applications)
                    if current_apps_count != last_apps_count or update_counter % 10 == 0:
                        await self.populate_app_list(app_list)
                        if page and hasattr(page, 'update'):
                            page.update()
                        last_apps_count = current_apps_count
                        print(f"🔄 UI обновлен: {current_apps_count} приложений")
                    
                    update_counter += 1
                    
                except Exception as e:
                    # Обработка ошибок интерфейса
                    self._handle_error('interface', e, f"Обновление UI #{update_counter}", show_user=False)
                    await asyncio.sleep(3.0)
            
            print("✅ Интерфейс расширенных настроек закрыт")
            
        except Exception as e:
            # Критическая ошибка интерфейса
            self._handle_error('interface', e, "Критическая ошибка интерфейса", show_user=True)
        finally:
            self._dialog_open = False

    def force_refresh_apps(self, app_list, page):
        """Принудительное обновление списка приложений."""
        try:
            print("🔄 Принудительное обновление списка приложений...")
            asyncio.create_task(self.populate_app_list(app_list))
            page.update()
        except Exception as e:
            print(f"⚠️ Ошибка принудительного обновления: {e}")

    async def populate_app_list(self, app_list):
        """Заполняет список приложений с оптимизацией производительности."""
        try:
            if not app_list or not hasattr(app_list, 'controls'):
                return
                
            # ОПТИМИЗАЦИЯ: Очищаем только если есть изменения
            current_apps_count = len(self.applications)
            if hasattr(app_list, '_last_apps_count') and app_list._last_apps_count == current_apps_count:
                return  # Нет изменений, не обновляем
            
            app_list.controls.clear()
            app_list._last_apps_count = current_apps_count
            
            if not self.applications:
                # Показываем сообщение если нет приложений
                app_list.controls.append(
                    ft.Container(
                        content=ft.Column([
                            ft.Icon("search_off", size=48, color="grey"),
                            ft.Text(
                                "Аудиоприложения не найдены", 
                                size=16, 
                                text_align=ft.TextAlign.CENTER,
                                color="grey"
                            ),
                            ft.Text(
                                "Запустите музыкальные проигрыватели, браузеры или другие аудиоприложения",
                                size=12,
                                text_align=ft.TextAlign.CENTER,
                                color="grey"
                            )
                        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                        padding=20,
                        alignment=ft.alignment.center
                    )
                )
                return
            
            # Группируем приложения по процессам для лучшей организации
            apps_by_process = {}
            for hwnd, info in self.applications.items():
                process_name = info['app_name']
                if process_name not in apps_by_process:
                    apps_by_process[process_name] = []
                apps_by_process[process_name].append((hwnd, info))
            
            # Создаем UI для каждой группы приложений
            for process_name, app_instances in apps_by_process.items():
                for hwnd, info in app_instances:
                    try:
                        # По умолчанию выбираем все устройства, если настройки не были сохранены
                        if info['title'] not in self.device_settings or not isinstance(self.device_settings[info['title']], list):
                            self.device_settings[info['title']] = self.devices.copy()

                        selected_devices = self.device_settings.get(info['title'], [])
                        
                        # УЛУЧШЕНИЕ: Более компактные чекбоксы
                        checkboxes = []
                        for device in self.devices:
                            try:
                                checkbox = ft.Checkbox(
                                    label=device,
                                    value=device in selected_devices,
                                    on_change=lambda e, app=info['title'], dev=device: self.update_device_selection(app, dev, e.control.value),
                                    scale=0.9  # Немного меньше размер
                                )
                                checkboxes.append(checkbox)
                            except Exception as e:
                                # Ошибка создания чекбокса не критична
                                continue

                        # УЛУЧШЕНИЕ: Более компактный контейнер для чекбоксов
                        checkboxes_container = ft.Container(
                            content=ft.Column(
                                controls=checkboxes,
                                scroll=ft.ScrollMode.AUTO,
                                spacing=5
                            ),
                            width=280,
                            height=min(120, len(checkboxes) * 35),  # Адаптивная высота
                            border=ft.border.all(1, "outline"),
                            border_radius=8,
                            padding=10,
                            bgcolor="surface_variant"
                        )

                        # УЛУЧШЕНИЕ: Более красивый дизайн элемента приложения
                        app_row = ft.Container(
                            content=ft.Column([
                                ft.Row([
                                    ft.Container(
                                        content=ft.Column([
                                            ft.Text(
                                                f"🎵 {info['title']}", 
                                                size=14, 
                                                weight=ft.FontWeight.W_500,
                                                max_lines=2,
                                                overflow=ft.TextOverflow.ELLIPSIS
                                            ),
                                            ft.Text(
                                                f"Процесс: {process_name}",
                                                size=11,
                                                color="outline"
                                            )
                                        ], spacing=2),
                                        expand=True
                                    ),
                                    checkboxes_container
                                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                                ft.Divider(height=1, color="outline")
                            ], spacing=10),
                            padding=ft.padding.symmetric(horizontal=10, vertical=8),
                            margin=ft.margin.symmetric(vertical=2),
                            border_radius=8,
                            bgcolor="surface",
                            border=ft.border.all(1, "outline")
                        )
                        
                        app_list.controls.append(app_row)
                        
                    except Exception as e:
                        # Ошибка создания UI для приложения
                        self._handle_error('interface', e, f"Создание UI для {info.get('title', 'Unknown')}", show_user=False)
                        continue
                        
        except Exception as e:
            # Критическая ошибка заполнения списка
            self._handle_error('interface', e, "Критическая ошибка заполнения списка", show_user=True)

    def close_dialog(self, page, dialog):
        """Закрывает диалог с правильной очисткой ресурсов."""
        print("❌ Закрытие диалога расширенных настроек...")
        try:
            self._dialog_open = False
            dialog.open = False
            
            # Очистка UI
            if hasattr(dialog, 'content') and hasattr(dialog.content, 'content'):
                if hasattr(dialog.content.content, 'controls'):
                    for control in dialog.content.content.controls:
                        if hasattr(control, 'content') and hasattr(control.content, 'controls'):
                            control.content.controls.clear()
            
            if page and hasattr(page, 'update'):
                page.update()
            print("✅ Диалог закрыт успешно")
        except Exception as e:
            print(f"⚠️ Ошибка закрытия диалога: {e}")

    def update_device_selection(self, app_name, device, is_selected):
        """Обновляет выбор устройства для приложения с улучшенной логикой."""
        try:
            selected_devices = self.device_settings.get(app_name, [])

            if isinstance(selected_devices, str):
                selected_devices = [selected_devices]

            if is_selected and device not in selected_devices:
                selected_devices.append(device)
                print(f"✅ Устройство '{device}' добавлено для '{app_name}'")
                
            elif not is_selected and device in selected_devices:
                selected_devices.remove(device)
                print(f"❌ Устройство '{device}' удалено для '{app_name}'")

            self.device_settings[app_name] = selected_devices
            self.save_settings()  # Сохраняем изменения
            
            print(f"💾 Настройки для '{app_name}': {selected_devices}")
            
            # ИСПРАВЛЕНИЕ: Уведомляем основную систему об изменениях
            if hasattr(self.app, 'on_routing_settings_changed'):
                self.app.on_routing_settings_changed(app_name, selected_devices)
            
        except Exception as e:
            self._handle_error('settings', e, f"Обновление настроек для {app_name}", show_user=False)

    def get_active_devices_for_current_app(self):
        """Возвращает список активных устройств для текущего активного приложения."""
        try:
            # Получаем активное окно
            active_hwnd = win32gui.GetForegroundWindow()
            
            for hwnd, info in self.applications.items():
                if hwnd == active_hwnd:
                    app_title = info['title']
                    return self.device_settings.get(app_title, self.devices.copy())
            
            # Если активное приложение не найдено, возвращаем все устройства
            return self.devices.copy()
            
        except Exception as e:
            self._handle_error('monitoring', e, "Определение активного приложения", show_user=False)
            return self.devices.copy()

    def should_route_to_device(self, device_name, target_app_title=None):
        """Проверяет должен ли звук маршрутизироваться на указанное устройство."""
        try:
            # Если нет настроек маршрутизации, разрешаем все устройства
            if not self.device_settings:
                return True
            
            # Если указано конкретное приложение
            if target_app_title:
                app_devices = self.device_settings.get(target_app_title, self.devices.copy())
                return device_name in app_devices if isinstance(app_devices, list) else True
            
            # Определяем активное приложение
            active_devices = self.get_active_devices_for_current_app()
            return device_name in active_devices if isinstance(active_devices, list) else True
            
        except Exception as e:
            self._handle_error('monitoring', e, f"Проверка маршрутизации для {device_name}", show_user=False)
            return True  # В случае ошибки разрешаем

    def get_routing_statistics(self):
        """Возвращает статистику маршрутизации."""
        try:
            total_apps = len(self.applications)
            configured_apps = len(self.device_settings)
            
            device_usage = {}
            for app_name, devices in self.device_settings.items():
                if isinstance(devices, list):
                    for device in devices:
                        device_usage[device] = device_usage.get(device, 0) + 1
            
            # Находим наиболее используемое устройство
            most_used_device = None
            if device_usage:
                max_usage = max(device_usage.values())
                for device, usage in device_usage.items():
                    if usage == max_usage:
                        most_used_device = device
                        break
            
            return {
                'total_apps': total_apps,
                'configured_apps': configured_apps,
                'device_usage': device_usage,
                'most_used_device': most_used_device
            }
            
        except Exception as e:
            self._handle_error('monitoring', e, "Получение статистики маршрутизации", show_user=False)
            return {}

    def export_settings(self, filepath=None):
        """Экспортирует настройки маршрутизации в файл."""
        try:
            if not filepath:
                filepath = f'audio_routing_backup_{int(time.time())}.json'
            
            export_data = {
                'version': '1.0',
                'timestamp': time.time(),
                'source_device': self.source_device_name,
                'devices': self.devices,
                'routing_settings': self.device_settings,
                'statistics': self.get_routing_statistics()
            }
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)
            
            print(f"💾 Настройки экспортированы в: {filepath}")
            return filepath
            
        except Exception as e:
            self._handle_error('settings', e, f"Экспорт настроек в {filepath}", show_user=True)
            return None

    def import_settings(self, filepath):
        """Импортирует настройки маршрутизации из файла."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                import_data = json.load(f)
            
            if 'routing_settings' in import_data:
                self.device_settings.update(import_data['routing_settings'])
                self.save_settings()
                print(f"📥 Настройки импортированы из: {filepath}")
                return True
            else:
                self._handle_error('settings', Exception("Неверный формат файла"), f"Импорт из {filepath}", show_user=True)
                return False
                
        except Exception as e:
            self._handle_error('settings', e, f"Импорт настроек из {filepath}", show_user=True)
            return False

    def reset_all_settings(self):
        """Сбрасывает все настройки маршрутизации."""
        try:
            self.device_settings.clear()
            self.save_settings()
            print("🔄 Все настройки маршрутизации сброшены")
            
            if hasattr(self.app, 'show_message'):
                self.app.show_message("✅ Настройки маршрутизации сброшены")
                
        except Exception as e:
            self._handle_error('settings', e, "Сброс всех настроек", show_user=True)

    async def start(self, page):
        """Запуск приложения и мониторинга с улучшенным контролем."""
        print("🚀 Запуск ApplicationAudioRouter...")
        try:
            # Сбрасываем состояние остановки
            self._stop_event.clear()
            
            # Запускаем задачи параллельно с контролем ошибок
            monitoring_task = asyncio.create_task(self.update_applications())
            interface_task = asyncio.create_task(self.show_interface(page))
            
            # Ждем завершения интерфейса (когда пользователь закроет диалог)
            await interface_task
            
            # Останавливаем мониторинг
            self._stop_event.set()
            
            # Ждем завершения мониторинга с таймаутом
            try:
                await asyncio.wait_for(monitoring_task, timeout=5.0)
            except asyncio.TimeoutError:
                print("⚠️ Мониторинг не завершился в течение таймаута, принудительная остановка")
                monitoring_task.cancel()
            
            print("✅ ApplicationAudioRouter завершен")
            
        except Exception as e:
            print(f"❌ Ошибка запуска маршрутизатора: {e}")
            self._stop_event.set()
        finally:
            # Гарантированная очистка
            self.stop_monitoring()

    def update_devices(self, new_devices):
        """Обновляет список устройств с синхронизацией настроек."""
        print(f"🔄 Обновление списка устройств: {len(new_devices)} устройств")
        
        # Удаляем настройки для несуществующих устройств
        for app_name, devices in list(self.device_settings.items()):
            if isinstance(devices, list):
                # Фильтруем только существующие устройства
                valid_devices = [d for d in devices if d in new_devices]
                if valid_devices != devices:
                    self.device_settings[app_name] = valid_devices
                    print(f"📱 Обновлены устройства для {app_name}: {valid_devices}")
        
        self.devices = new_devices
        self.save_settings()
        print(f"✅ Список устройств обновлен: {self.devices}")

    def update_source_device(self, source_device_name):
        """Обновляет источник звука для маршрутизации."""
        self.source_device_name = source_device_name
        if source_device_name and hasattr(self.app, 'get_device_id'):
            self.source_device_id = self.app.get_device_id(source_device_name)
            print(f"🎤 Источник звука обновлен: {source_device_name} (ID: {self.source_device_id})")
        else:
            self.source_device_id = None
            print(f"❌ Источник звука сброшен")

    def get_device_settings_for_app(self, app_title):
        """Получает настройки устройств для конкретного приложения."""
        return self.device_settings.get(app_title, self.devices.copy())

    def is_device_enabled_for_app(self, app_title, device_name):
        """Проверяет включено ли устройство для конкретного приложения."""
        app_devices = self.device_settings.get(app_title, self.devices.copy())
        return device_name in app_devices if isinstance(app_devices, list) else False

    def _handle_error(self, error_type: str, error: Exception, context: str = "", show_user: bool = False):
        """Централизованная обработка ошибок с логированием и уведомлениями."""
        current_time = time.time()
        
        # Сброс счетчиков ошибок если прошло достаточно времени
        if current_time - self.last_error_time > self.error_reset_interval:
            self.error_counts = {key: 0 for key in self.error_counts.keys()}
            print("📊 Счетчики ошибок сброшены")
        
        # Увеличиваем счетчик ошибок для данного типа
        if error_type in self.error_counts:
            self.error_counts[error_type] += 1
        
        self.last_error_time = current_time
        
        # Формируем сообщение об ошибке
        error_id = f"{error_type}_{current_time:.0f}"
        error_msg = f"[{error_type.upper()}] {str(error)}"
        
        if context:
            error_msg += f" | Контекст: {context}"
        
        print(f"❌ {error_msg} (ID: {error_id})")
        
        # Проверяем критичность ошибки
        is_critical = self.error_counts.get(error_type, 0) > self.max_errors_per_category
        
        if is_critical:
            print(f"🚨 КРИТИЧЕСКАЯ ОШИБКА: {error_type} превысил лимит ({self.max_errors_per_category})")
            
            # Показываем критическую ошибку пользователю
            if hasattr(self.app, 'show_message'):
                self.app.show_message(
                    f"🚨 Критическая ошибка в расширенных настройках!\n\n"
                    f"Тип: {error_type}\n"
                    f"Описание: {str(error)[:100]}...\n\n"
                    f"Рекомендация: Перезапустите приложение"
                )
            
            # Принудительно останавливаем проблемный компонент
            self._emergency_stop()
            
        elif show_user and hasattr(self.app, 'show_message'):
            # Показываем обычную ошибку пользователю
            self.app.show_message(
                f"⚠️ Ошибка в расширенных настройках\n\n"
                f"{str(error)}\n\n"
                f"Попробуйте повторить операцию"
            )
        
        return error_id

    def _emergency_stop(self):
        """Экстренная остановка при критических ошибках."""
        print("🚨 Экстренная остановка ApplicationAudioRouter...")
        try:
            self._stop_event.set()
            self._dialog_open = False
            self.stop_monitoring()
            print("✅ Экстренная остановка выполнена")
        except Exception as e:
            print(f"❌ Ошибка экстренной остановки: {e}")

    def _is_error_critical(self, error: Exception) -> bool:
        """Определяет критичность ошибки."""
        critical_errors = [
            'MemoryError',
            'SystemError', 
            'OSError',
            'PermissionError',
            'FileNotFoundError'
        ]
        return any(err in str(type(error).__name__) for err in critical_errors)

    def _validate_state(self) -> tuple[bool, str]:
        """Проверяет текущее состояние на валидность."""
        if not self.devices:
            return False, "Нет доступных устройств"
        
        if not self.source_device_name:
            return False, "Не выбран источник звука"
        
        if self._stop_event.is_set():
            return False, "Система остановлена"
        
        # Проверяем превышение лимитов ошибок
        for error_type, count in self.error_counts.items():
            if count > self.max_errors_per_category:
                return False, f"Превышен лимит ошибок для {error_type}"
        
        return True, "Состояние валидно"
