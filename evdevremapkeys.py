#!/usr/bin/env python3
#
# Copyright (c) 2017 Philip Langdale
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


import argparse
import asyncio
import functools
from pathlib import Path
import signal
import sys

import daemon
import evdev
from evdev import ecodes, InputDevice, UInput
from xdg import BaseDirectory
import yaml

try:
    import Xlib
    import Xlib.display
except ImportError:
    pass


DEBUG = False
DEFAULT_RATE = .1  # seconds
repeat_tasks = {}
remapped_tasks = {}
active_remapped_keys = {}  # Keys activated as a result of remapping
active_output_keys = {}  # Output keys currently active
active_input_keys = {}  # Input keys currently active


def write_event(output, event):
    if event.type == ecodes.EV_KEY:
        if DEBUG:
            print("OUT", event)
        if event.value is 0:
            active_output_keys[output.number].discard(event.code)
        elif event.value is 1:
            active_output_keys[output.number].add(event.code)
    output.write_event(event)
    output.syn()


def get_active_window():
    try:
        # Try to get display the first time, then cache it
        if get_active_window.display is None:
            get_active_window.display = Xlib.display.Display()
        root = get_active_window.display.screen().root
        NET_ACTIVE_WINDOW = get_active_window.display.intern_atom('_NET_ACTIVE_WINDOW')
        win_id = root.get_full_property(NET_ACTIVE_WINDOW,
                                        Xlib.X.AnyPropertyType).value[0]
        window_obj = get_active_window.display.create_resource_object('window', win_id)
        cls = window_obj.get_wm_class() if window_obj else None
        return cls[1] if cls else None
    except (Xlib.error.DisplayConnectionError, Xlib.error.BadWindow, BrokenPipeError):
        return None


@asyncio.coroutine
def handle_events(input, output, remappings, critical):
    while True:
        events = yield from input.async_read()  # noqa
        try:
            for event in events:
                best_remapping = ([], None)  # (keys, remapping)
                if event.type == ecodes.EV_KEY:
                    if DEBUG:
                        print("IN", event, active_input_keys,
                              active_output_keys, active_remapped_keys)
                    if event.value is 0:
                        active_input_keys[input.number].discard(event.code)
                    elif event.value is 1:
                        active_input_keys[input.number].add(event.code)
                    active_keys = active_input_keys[input.number].copy()
                    active_keys.add(event.code)  # Needed to include code on keyup
                    # Check if there is any possible match excluding window class
                    # This way we save CPU by not checking window class every time
                    if any(active_keys.issuperset(
                            k for k in keys if isinstance(k, int))
                           for keys in remappings):
                        if get_active_window.display is not False:
                            # Use window class to select mapping
                            active_keys.add(get_active_window())
                        for keys, remapping in remappings.items():
                            if active_keys.issuperset(keys) and \
                               len(keys) > len(best_remapping[0]):
                                best_remapping = (keys, remapping)
                if best_remapping[1] and event.code in best_remapping[0]:
                    remap_event(output, event,
                                best_remapping[0], best_remapping[1])
                else:
                    # Re-press any input keys that were released when
                    # a remapping was activated
                    if event.type == ecodes.EV_KEY and event.value is 1:
                        press_input_keys(input, output, event)
                    write_event(output, event)
        except OSError as e:
            if critical:
                print("Error for critical device '%s' (%s). Quitting." % (input.name, e))
                sys.exit(1)
            else:
                print("Device error for '%s' (%s). Ignoring." % (input.name, e))
                return


@asyncio.coroutine
def repeat_event(event, rate, count, values, output):
    if count == 0:
        count = -1
    while count is not 0:
        count -= 1
        for value in values:
            event.value = value
            write_event(output, event)

        yield from asyncio.sleep(rate)


def release_output_keys(output, cur_event, keys, remappings):
    # Release input keys that got activated before remapping activated
    to_release = set(key for key in keys if isinstance(key, int))
    to_release.discard(cur_event.code)
    # But do not release keys that will be re-activated as part of remapping
    to_release -= set(r['code'] for r in remappings)
    # Release keys activated due to any previously active remapping
    to_release |= active_remapped_keys[output.number]
    # Only release keys that are actually pressed at the moment
    to_release &= active_output_keys[output.number]
    for key in to_release:
        active_remapped_keys[output.number].discard(key)
        event = evdev.events.InputEvent(cur_event.sec, cur_event.usec,
                                        ecodes.EV_KEY, key, 0)
        write_event(output, event)


def press_input_keys(input, output, cur_event):
    # Reactivate any inactive, pressed input keys
    for key in (active_input_keys[input.number] -
                active_output_keys[output.number]):
        event = evdev.events.InputEvent(cur_event.sec, cur_event.usec,
                                        ecodes.EV_KEY, key, 1)
        write_event(output, event)


def remap_event(output, event, keys, remappings):
    key_down = event.value is 1
    key_up = event.value is 0
    if key_down:
        release_output_keys(output, event, keys, remappings)
    for remapping in remappings:
        original_code = event.code
        event.code = remapping['code']
        event.type = remapping.get('type', None) or event.type
        values = remapping.get('value', None) or [event.value]
        repeat = remapping.get('repeat', False)
        delay = remapping.get('delay', False)
        if not repeat and not delay:
            for value in values:
                event.value = value
                if value is 1:
                    if event.code not in active_output_keys[output.number]:
                        active_remapped_keys[output.number].add(event.code)
                        write_event(output, event)
                elif value is 0:
                    # Do not release keys that were not activated as part of
                    # the remapping unless its the key being released
                    if (event.code in active_output_keys[output.number] and
                        (event.code in active_remapped_keys[output.number] or
                         event.code == original_code)):
                        active_remapped_keys[output.number].discard(event.code)
                        write_event(output, event)
                else:
                    write_event(output, event)
        else:
            count = remapping.get('count', 0)

            if not (key_up or key_down):
                return
            if delay:
                if keys not in remapped_tasks or remapped_tasks[keys] == 0:
                    if key_down:
                        remapped_tasks[keys] = count
                else:
                    if key_down:
                        remapped_tasks[keys] -= 1

                if remapped_tasks[keys] == count:
                    write_event(output, event)
            elif repeat:
                # count > 0  - ignore key-up events
                # count is 0 - repeat until key-up occurs
                ignore_key_up = count > 0

                if ignore_key_up and key_up:
                    return
                rate = remapping.get('rate', DEFAULT_RATE)
                repeat_task = repeat_tasks.pop(keys, None)
                if repeat_task:
                    repeat_task.cancel()
                if key_down:
                    repeat_tasks[keys] = asyncio.ensure_future(
                        repeat_event(event, rate, count, values, output))


# Parses yaml config file and outputs normalized configuration.
# Sample output:
#  'devices': [{
#    'input_fn': '',
#    'input_name': '',
#    'input_phys': '',
#    'output_name': '',
#    'remappings': {
#      42: [{             # Matched key/button code
#        'code': 30,      # Mapped key/button code
#        'type': EV_REL,  # Overrides received event type [optional]
#                         # Defaults to EV_KEY
#        'value': [1, 0], # Overrides received event value [optional].
#                         # If multiple values are specified they will
#                         # be applied in sequence.
#                         # Defaults to the value of received event.
#        'repeat': True,  # Repeat key/button code [optional, default:False]
#        'delay': True,   # Delay key/button output [optional, default:False]
#        'rate': 0.2,     # Repeat rate in seconds [optional, default:0.1]
#        'count': 3       # Repeat/Delay counter [optional, default:0]
#                         # For repeat:
#                         # If count is 0 it will repeat until key/button is depressed
#                         # If count > 0 it will repeat specified number of times
#                         # For delay:
#                         # Will suppress key/button output x times before execution [x = count]
#                         # Ex: count = 1 will execute key press every other time
#      }]
#    }
#  }]
def load_config(config_override):
    conf_path = None
    if config_override is None:
        for dir in BaseDirectory.load_config_paths('evdevremapkeys'):
            conf_path = Path(dir) / 'config.yaml'
            if conf_path.is_file():
                break
        if conf_path is None:
            raise NameError('No config.yaml found')
    else:
        conf_path = Path(config_override)
        if not conf_path.is_file():
            raise NameError('Cannot open %s' % config_override)

    with open(conf_path.as_posix(), 'r') as fd:
        config = yaml.safe_load(fd)
        for device in config['devices']:
            device['remappings'] = normalize_config(device['remappings'])
            device['remappings'] = resolve_ecodes(device['remappings'])

    return config


# Converts general config schema
# {'remappings': {
#     'BTN_EXTRA': [
#         'KEY_Z',
#         'KEY_A',
#         {'code': 'KEY_X', 'value': 1}
#         {'code': 'KEY_Y', 'value': [1,0]]}
#     ],
#     '(KEY_LEFTMETA, BTN_EXTRA)': [
#         'KEY_Z',
#         'KEY_A',
#         {'code': 'KEY_X', 'value': 1}
#         {'code': 'KEY_Y', 'value': [1,0]]}
#     ]
# }}
# into fixed format
# {'remappings': {
#     ('BTN_EXTRA',): [
#         {'code': 'KEY_Z'},
#         {'code': 'KEY_A'},
#         {'code': 'KEY_X', 'value': [1]}
#         {'code': 'KEY_Y', 'value': [1,0]]}
#     ],
#     ('KEY_LEFTMETA', 'BTN_EXTRA'): [
#         {'code': 'KEY_Z'},
#         {'code': 'KEY_A'},
#         {'code': 'KEY_X', 'value': [1]}
#         {'code': 'KEY_Y', 'value': [1,0]]}
#     ]
# }}
def normalize_config(remappings):
    norm = {}
    for keys, mappings in remappings.items():
        if keys.startswith('(') and keys.endswith(')'):
            keys = tuple(k.strip() for k in keys.strip('()').split(','))
        else:
            keys = (keys,)
        new_mappings = []
        for mapping in mappings:
            if type(mapping) is str:
                new_mappings.append({'code': mapping})
            else:
                normalize_value(mapping)
                new_mappings.append(mapping)
        norm[keys] = new_mappings
    return norm


def normalize_value(mapping):
    value = mapping.get('value')
    if value is None or type(value) is list:
        return
    mapping['value'] = [mapping['value']]


def resolve_ecodes(by_name):
    def resolve_mapping(mapping):
        if 'code' in mapping:
            mapping['code'] = ecodes.ecodes[mapping['code']]
        if 'type' in mapping:
            mapping['type'] = ecodes.ecodes[mapping['type']]
        return mapping
    return {tuple(ecodes.ecodes[key] if key in ecodes.ecodes else key
                  for key in keys):
            list(map(resolve_mapping, mappings))
            for keys, mappings in by_name.items()}


def find_input(device):
    name = device.get('input_name', None)
    phys = device.get('input_phys', None)
    fn = device.get('input_fn', None)

    if name is None and phys is None and fn is None:
        raise NameError('Devices must be identified by at least one ' +
                        'of "input_name", "input_phys", or "input_fn"')

    devices = [InputDevice(fn) for fn in evdev.list_devices()]
    for input in devices:
        if name is not None and input.name != name:
            continue
        if phys is not None and input.phys != phys:
            continue
        if fn is not None and input.fn != fn:
            continue
        return input
    return None


def register_device(device, device_number):
    critical = device.get('critical', False)

    input = find_input(device)
    if input is None:
        if critical:
            print("Can't find critical input device '%s'. Quitting." %
                  (device.get('input_name', None) or
                   device.get('input_phys', None) or
                   device.get('input_fn', None)))
            sys.exit(1)
        else:
            print("Can't find input device '%s'. Ignoring." %
                  (device.get('input_name', None) or
                   device.get('input_phys', None) or
                   device.get('input_fn', None)))
            return
    input.grab()
    input.number = device_number

    caps = input.capabilities()
    # EV_SYN is automatically added to uinput devices
    del caps[ecodes.EV_SYN]

    remappings = device['remappings']
    extended = set(caps[ecodes.EV_KEY])

    def flatmap(lst):
        return [l2 for l1 in lst for l2 in l1]
    extended.update([remapping['code'] for remapping in flatmap(remappings.values())])
    caps[ecodes.EV_KEY] = list(extended)

    output = UInput(caps, name=device['output_name'])
    output.number = device_number

    active_remapped_keys[output.number] = set()
    active_output_keys[output.number] = set()
    active_input_keys[input.number] = set()

    asyncio.ensure_future(handle_events(input, output, remappings, critical))


@asyncio.coroutine
def shutdown(loop):
    tasks = [task for task in asyncio.Task.all_tasks() if task is not
             asyncio.tasks.Task.current_task()]
    list(map(lambda task: task.cancel(), tasks))
    yield from asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


def run_loop(args):
    if 'Xlib' in sys.modules:
        get_active_window.display = None
    else:
        get_active_window.display = False
        print("XLib not found. Active window class will be ignored when matching remappings.")

    config = load_config(args.config_file)
    for i, device in enumerate(config['devices']):
        register_device(device, i)

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM,
                            functools.partial(asyncio.ensure_future,
                                              shutdown(loop)))

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        loop.remove_signal_handler(signal.SIGTERM)
        loop.run_until_complete(asyncio.ensure_future(shutdown(loop)))
    finally:
        loop.close()


def list_devices():
    devices = [InputDevice(fn) for fn in evdev.list_devices()]
    for device in reversed(devices):
        yield [device.fn, device.phys, device.name]


def read_events(req_device):
    for device in list_devices():
        # Look in all 3 identifiers + event number
        if req_device in device or req_device == device[0].replace("/dev/input/event", ""):
            found = evdev.InputDevice(device[0])

    if 'found' not in locals():
        print("Device not found. \nPlease use --list-devices to view a list of available devices.")
        return

    print(found)
    print("To stop, press Ctrl-C")

    for event in found.read_loop():
        try:
            if event.type == evdev.ecodes.EV_KEY:
                categorized = evdev.categorize(event)
                if categorized.keystate == 1:
                    keycode = categorized.keycode if type(categorized.keycode) is str else \
                        " | ".join(categorized.keycode)
                    print("Key pressed: %s (%s)" % (keycode, categorized.scancode))
        except KeyError:
            if event.value:
                print("Unknown key (%s) has been pressed." % event.code)
            else:
                print("Unknown key (%s) has been released." % event.code)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Re-bind keys for input devices')
    parser.add_argument('-d', '--daemon',
                        help='Run as a daemon', action='store_true')
    parser.add_argument('-f', '--config-file',
                        help='Config file that overrides default location')
    parser.add_argument('-l', '--list-devices', action='store_true',
                        help='List input devices by name and physical address')
    parser.add_argument('-e', '--read-events', metavar='EVENT_ID',
                        help='Read events from an input device by either name, physical address or number.')

    args = parser.parse_args()
    if args.list_devices:
        print("\n".join(['%s:\t"%s" | "%s' % (fn, phys, name)
                         for (fn, phys, name) in list_devices()]))
    elif args.read_events:
        read_events(args.read_events)
    elif args.daemon:
        with daemon.DaemonContext():
            run_loop(args)
    else:
        run_loop(args)
