#!/usr/bin/env python
import sys

from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QSystemTrayIcon, QMenu, QAction, QWidget, QWidgetAction, QSlider
from PyQt5.QtCore import Qt
from fbs_runtime.application_context.PyQt5 import ApplicationContext
from shutil import copyfile
from functools import partial
import contextlib
import os
import pulsectl
import argparse

pulse = pulsectl.Pulse("t")


class CadmusBackendApp:
    def __init__(self, app_context):
        self.audio_sources = []
        self.cadmus_lib_path = ""
        self.app_context = app_context
        self.drop_cadmus_binary()

    def sources_list(self):
        if len(self.audio_sources) > 0:
            return self.audio_sources.copy()
        for src in pulse.source_list():
            self.audio_sources.append(src)
        return self.audio_sources.copy()

    def drop_cadmus_binary(self):
        cadmus_cache_path = os.path.join(os.environ["HOME"], ".cache", "cadmus")
        if not os.path.exists(cadmus_cache_path):
            os.makedirs(cadmus_cache_path)

        self.cadmus_lib_path = os.path.join(cadmus_cache_path, "librnnoise_ladspa.so")

        copyfile(
            self.app_context.get_resource("librnnoise_ladspa.so"), self.cadmus_lib_path
        )

    def disable_noise_suppression(self):
        CadmusPulseInterface.unload_modules()

    def enable_noise_suppression(self, mic_name, control_level):
        if mic_name not in [src.name for src in self.sources_list()]:
            raise Exception(f"Unknown mic name {mic_name}")
        CadmusPulseInterface.load_modules(mic_name, control_level, self.cadmus_lib_path)

class CadmusPulseInterface:
    @staticmethod
    def cli_command(command):
        if not isinstance(command, list):
            command = [command]
        with contextlib.closing(pulsectl.connect_to_cli()) as s:
            for c in command:
                s.write(c + "\n")

    @staticmethod
    def load_modules(mic_name, control_level, cadmus_lib_path):


        control_level = max(control_level, 50)

        pulse.module_load(
            "module-null-sink",
            "sink_name=mic_denoised_out "
            "sink_properties=\"device.description='Cadmus Microphone Sink'\"",
        )
        pulse.module_load(
            "module-ladspa-sink",
            "sink_name=mic_raw_in sink_master=mic_denoised_out label=noise_suppressor_mono plugin=%s control=%d "
            "sink_properties=\"device.description='Cadmus Raw Microphone Redirect'\""
            % (cadmus_lib_path, control_level),
        )

        pulse.module_load(
            "module-loopback",
            "latency_msec=1 source=%s sink=mic_raw_in channels=1" % mic_name,
        )

        pulse.module_load(
            "module-remap-source",
            "master=mic_denoised_out.monitor source_name=denoised "
            "source_properties=\"device.description='Cadmus Denoised Microphone (Use me!)'\"",
        )

        print("Set suppression level to %d" % control_level)

    @staticmethod
    def unload_modules():
        CadmusPulseInterface.cli_command(
            [
                "unload-module module-loopback",
                "unload-module module-null-sink",
                "unload-module module-ladspa-sink",
                "unload-module module-remap-source",
            ]
        )


class AudioMenuItem(QAction):
    def __init__(self, text, parent, mic_name):
        super().__init__(text, parent)
        self.mic_name = mic_name
        self.setStatusTip("Use the %s as an input for noise suppression" % text)


class CadmusApplication(QSystemTrayIcon):
    control_level = 50

    def __init__(self, app_context, parent=None):
        QSystemTrayIcon.__init__(self, parent)
        self.app_context = app_context
        self.enabled_icon = QIcon(app_context.get_resource("icon_enabled.png"))
        self.disabled_icon = QIcon(app_context.get_resource("icon_disabled.png"))

        self.disable_suppression_menu = QAction("Disable Noise Suppression")
        self.enable_suppression_menu = QMenu("Enable Noise Suppression")
        self.level_section = None
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setTickInterval(5)
        self.slider.setMinimum(0)
        self.slider.setMaximum(100)
        self.slider.setValue(CadmusApplication.control_level)
        self.slider.valueChanged.connect(self.slider_valuechange)
        self.exit_menu = QAction("Exit")

        self.gui_setup()
        self.drop_cadmus_binary()

    def get_section_message(self):
        return "Suppression Level: %d" % self.slider.value()

    def slider_valuechange(self):
        CadmusApplication.control_level = self.slider.value()
        self.level_section.setText(self.get_section_message())

    def gui_setup(self):
        main_menu = QMenu()

        for src in self.backend_app.source_list():
            mic_menu_item = AudioMenuItem(
                src.description, self.enable_suppression_menu, src.name,
            )
            self.audio_sources.append(src)
            self.enable_suppression_menu.addAction(mic_menu_item)
            mic_menu_item.triggered.connect(partial(self.backend_app.enable_noise_suppression, mic_name=mic_menu_item.name))

        self.disable_suppression_menu.setEnabled(False)
        self.disable_suppression_menu.triggered.connect(self.backend_app.disable_noise_suppression)

        self.exit_menu.triggered.connect(self.quit)

        main_menu.addMenu(self.enable_suppression_menu)
        main_menu.addAction(self.backend_app.disable_suppression_menu)
        main_menu.addAction(self.exit_menu)

        # Add slider widget
        self.level_section = self.enable_suppression_menu.addSection(self.get_section_message())
        wa = QWidgetAction(self.enable_suppression_menu)
        wa.setDefaultWidget(self.slider)
        self.enable_suppression_menu.addAction(wa)

        self.setIcon(self.disabled_icon)
        self.setContextMenu(main_menu)

    def quit(self):
        self.backend_app.disable_noise_suppression()
        self.app_context.app.quit()



class CadmusApplicationCli:
    def __init__(self, app_context):
        self.backend_app = CadmusBackendApp(app_context)
        self.parser = argparse.ArgumentParser(description="Noise Suppression cli")
        subparsers = self.parser.add_subparsers(help='sub-command help', dest='cmd')
        subparser_sources = subparsers.add_parser('sources', help='list the audio sources available')
        subparser_enable  = subparsers.add_parser('enable', help='enable noise suppression for a specific source')
        subparser_disable = subparsers.add_parser('disable', help='disable noise suppression')
        subparser_enable.add_argument('source', help='The source to enable', \
                                      choices=[src.name for src in self.backend_app.sources_list()])
        self.args = self.parser.parse_args()
    def run(self):
        if self.args.cmd == 'sources':
            self.print_sources()
        elif self.args.cmd == 'enable':
            self.backend_app.enable_noise_suppression(self.args.source, 50)
        elif self.args.cmd == 'disable':
            self.backend_app.disable_noise_suppression()
    def print_sources(self):
        print('Current sources:')
        for src in self.backend_app.sources_list():
            print('-----------------------------')
            print(f'name = {src.name}')
            print(f'description = {src.description}')

if __name__ == "__main__":
    context = ApplicationContext()
    app = CadmusApplicationCli(context)
    app.run()
