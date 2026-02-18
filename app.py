import serial
import time
import threading
import atexit
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import logging
from collections import deque
import json # For handling settings file
import os   # For checking file/directory existence

# --- Configuration Defaults ---
DEFAULT_SETTINGS = {
    "serial_port": "/dev/ttyUSB0", # Keep serial port configurable here if needed, or hardcode
    "baud_rate": 9600,
    "threshold_ceiling": 49.5,
    "threshold_floor": 45.0,
    "command_open_hex": "A00101A2",
    "command_close_hex": "A00100A1",
    "temp_path": "/sys/class/hwmon/hwmon0/temp1_input",
    "check_interval_seconds": 2,
    "history_duration_hours": 24 # Default maximum history duration
}
# --- Persistent Configuration File Path (inside container) ---
# --- Persistent Configuration File Path (inside container) ---
CONFIG_DIR = os.getenv("CONFIG_DIR", "/config")
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, "settings.json")

# --- Web Server Port ---
WEB_PORT = int(os.getenv("WEB_PORT", 4812))

# --- Global Variables & Shared State ---
current_settings = {} # Loaded from file or defaults
fan_state = False  # False = Off, True = On
current_temp = None # Store the latest temperature reading
last_error = None # Store the last error message
ser = None # Serial object
control_thread = None
stop_thread = threading.Event()
fan_history = deque() # Store (timestamp, state) tuples - state is boolean True/False
state_lock = threading.Lock() # Lock for fan_state, fan_history, current_temp, last_error
settings_lock = threading.Lock() # Separate lock for settings dictionary R/W
command_open_bytes = b'' # Derived from current_settings
command_close_bytes = b'' # Derived from current_settings
history_duration = timedelta(hours=DEFAULT_SETTINGS["history_duration_hours"]) # Derived from settings
check_interval = DEFAULT_SETTINGS["check_interval_seconds"] # Derived from settings


# --- Logging Setup ---
logging.basicConfig(filename='fan.log', # Log file location can also be configurable
                    level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

def write_log(level, message):
    """Logs messages to file and console."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}") # Also print to console for Docker logs
    if level == 'info':
        logging.info(message)
    elif level == 'warning':
        logging.warning(message)
    elif level == 'error':
        logging.error(message)

# --- Settings Persistence ---
def load_settings():
    """Loads settings from JSON file, using defaults if file/key is missing."""
    global current_settings, command_open_bytes, command_close_bytes, history_duration, check_interval
    write_log('info', f"Attempting to load settings from {CONFIG_FILE_PATH}")
    loaded_successfully = False
    settings_temp = DEFAULT_SETTINGS.copy() # Start with defaults

    try:
        if os.path.exists(CONFIG_FILE_PATH):
            with open(CONFIG_FILE_PATH, 'r') as f:
                loaded_settings = json.load(f)
            # Update defaults with loaded values, ensuring only expected keys are updated or handled
            # Use .get() with default to handle missing keys gracefully
            for key in DEFAULT_SETTINGS:
                # Explicitly handle potential type issues from JSON (e.g., numbers as strings)
                if key in ['threshold_ceiling', 'threshold_floor']:
                    try: settings_temp[key] = float(loaded_settings.get(key, DEFAULT_SETTINGS[key]))
                    except (ValueError, TypeError): write_log('warning', f"Invalid value for setting '{key}': {loaded_settings.get(key)}. Using default {DEFAULT_SETTINGS[key]}."); settings_temp[key] = DEFAULT_SETTINGS[key]
                elif key in ['check_interval_seconds', 'history_duration_hours']:
                    try: settings_temp[key] = int(loaded_settings.get(key, DEFAULT_SETTINGS[key]))
                    except (ValueError, TypeError): write_log('warning', f"Invalid value for setting '{key}': {loaded_settings.get(key)}. Using default {DEFAULT_SETTINGS[key]}."); settings_temp[key] = DEFAULT_SETTINGS[key]
                else: # For strings like paths, commands, port
                    settings_temp[key] = loaded_settings.get(key, DEFAULT_SETTINGS[key])


            write_log('info', "Successfully loaded settings from file.")
            loaded_successfully = True
        else:
            write_log('warning', f"Config file not found. Using default settings.")
            # settings_temp is already initialized with DEFAULT_SETTINGS
            # Attempt to save defaults immediately
            save_settings(settings_temp) # Save defaults if file is missing

    except (json.JSONDecodeError, IOError, Exception) as e:
        write_log('error', f"Error loading {CONFIG_FILE_PATH}: {e}. Using default settings.")
        # settings_temp is already initialized with DEFAULT_SETTINGS

    with settings_lock: # Lock before assigning to global
        current_settings = settings_temp
        # Derive dependent variables - ensure types are correct
        # Ensure values are positive where required
        history_duration_hours_val = max(1, int(current_settings.get("history_duration_hours", DEFAULT_SETTINGS["history_duration_hours"]))) # Ensure at least 1 hour
        check_interval_seconds_val = max(1, int(current_settings.get("check_interval_seconds", DEFAULT_SETTINGS["check_interval_seconds"]))) # Ensure at least 1 second

        history_duration = timedelta(hours=history_duration_hours_val)
        check_interval = check_interval_seconds_val

        current_settings["history_duration_hours"] = history_duration_hours_val # Update settings dict with valid values
        current_settings["check_interval_seconds"] = check_interval_seconds_val

        # Derive byte commands safely
        derive_byte_commands(update_global=True) # Update globals command_open/close_bytes

    return loaded_successfully


def save_settings(settings_dict):
    """Saves the provided settings dictionary to the JSON file."""
    write_log('info', f"Attempting to save settings to {CONFIG_FILE_PATH}")
    try:
        # Ensure config directory exists
        os.makedirs(CONFIG_DIR, exist_ok=True)
        # Validate dictionary keys against defaults before saving if needed,
        # but for simplicity, we save whatever valid settings_dict is passed.
        with open(CONFIG_FILE_PATH, 'w') as f:
            json.dump(settings_dict, f, indent=4)
        write_log('info', f"Settings successfully saved.")
        return True
    except (IOError, Exception) as e:
        write_log('error', f"Failed to save settings to {CONFIG_FILE_PATH}: {e}")
        return False

def derive_byte_commands(settings_dict=None, update_global=False):
    """
    Updates byte commands from hex strings.
    If settings_dict is provided, uses that dict. Otherwise uses global current_settings.
    If update_global is True, updates global command_open/close_bytes.
    Returns tuple (open_bytes, close_bytes) or (None, None) on error for the *provided* hex strings.
    The global commands are updated even on error (to defaults).
    """
    global command_open_bytes, command_close_bytes # Needed if update_global is True

    source_settings = settings_dict if settings_dict is not None else current_settings
    open_hex = source_settings.get("command_open_hex", DEFAULT_SETTINGS["command_open_hex"])
    close_hex = source_settings.get("command_close_hex", DEFAULT_SETTINGS["command_close_hex"])

    open_b_derived = None
    close_b_derived = None
    original_values_valid = True

    try:
        open_b_derived = bytes.fromhex(open_hex)
    except (ValueError, TypeError):
        write_log('error', f"Invalid hex format for OPEN command: {open_hex}. Using default.")
        original_values_valid = False
        open_b_derived = bytes.fromhex(DEFAULT_SETTINGS["command_open_hex"]) # Use default bytes


    try:
        close_b_derived = bytes.fromhex(close_hex)
    except (ValueError, TypeError):
        write_log('error', f"Invalid hex format for CLOSE command: {close_hex}. Using default.")
        original_values_valid = False
        close_b_derived = bytes.fromhex(DEFAULT_SETTINGS["command_close_hex"]) # Use default bytes

    if update_global:
        command_open_bytes = open_b_derived
        command_close_bytes = close_b_derived

    # Return bytes derived from the *provided* hex, indicating if they were valid
    return (open_b_derived, close_b_derived) if original_values_valid else (None, None)

# --- Serial Communication ---
def init_serial():
    """Initializes the serial connection based on current settings."""
    global ser, last_error
    port = current_settings.get("serial_port", DEFAULT_SETTINGS["serial_port"])
    rate = current_settings.get("baud_rate", DEFAULT_SETTINGS["baud_rate"])

    # Close existing connection if open
    if ser and ser.is_open:
        try:
            ser.close()
            write_log('info', f"Closed existing serial port connection.")
        except Exception as e:
            write_log('warning', f"Error closing serial port: {e}")
        ser = None # Reset serial object

    write_log('info', f"Attempting to open serial port {port} at {rate} baud.")
    try:
        ser = serial.Serial(port, rate, timeout=1)
        write_log('info', f"Serial port {port} opened successfully.")
        with state_lock: # Use state_lock for last_error consistency
            # Only clear serial-specific errors on success
            if last_error is not None and "serial port" in last_error.lower():
                last_error = None
            # Consider if we should clear *any* error on successful serial init. Maybe not.
        return True
    except serial.SerialException as e:
        error_msg = f"Cannot open serial port {port}: {e}"
        write_log('error', error_msg)
        ser = None # Ensure ser is None if connection failed
        with state_lock:
            last_error = error_msg
        return False
    except Exception as e:
        error_msg = f"Unexpected error opening serial port {port}: {e}"
        write_log('error', error_msg)
        ser = None
        with state_lock:
            last_error = error_msg
        return False

def set_fan(state):
    """Controls the USB relay fan using derived byte commands."""
    global ser, last_error # Need global ser and last_error here too
    # Use state_lock for modifying last_error
    # Use settings_lock for reading command bytes (though they rarely change)

    with settings_lock: # Ensure we read the latest derived commands
        cmd_open = command_open_bytes
        cmd_close = command_close_bytes

    # Ensure commands are not None (should be handled by derive_byte_commands using defaults)
    if cmd_open is None or cmd_close is None:
        write_log('error', "Fan commands not derived successfully. Cannot set fan state.")
        with state_lock:
            last_error = "Internal error: Invalid fan commands."
        return False


    command = cmd_open if state else cmd_close
    action = "ON" if state else "OFF"

    if ser is None or not ser.is_open:
        write_log('warning', f"Serial port not available trying to turn fan {action}. Attempting reconnect.")
        if not init_serial():
            # Error message already set by init_serial
            return False # Indicate failure
        # If reconnect succeeds, continue

    try:
        ser.write(command)
        time.sleep(0.2) # Delay for relay to act
        write_log('info', f"Sent command {command.hex()} to turn fan {action}.")
        with state_lock:
            # Only clear serial send errors on success, leave others
            if last_error is not None and ("Serial error sending" in last_error or "Error sending" in last_error):
                last_error = None
        return True # Indicate success
    except serial.SerialException as e:
        error_msg = f"Serial error sending {action} command: {e}"
        write_log('error', error_msg)
        with state_lock:
            last_error = error_msg
        # Close potentially broken port
        if ser:
            try:
                ser.close()
            except Exception as close_e:
                write_log('warning', f"Error during serial port close after send error: {close_e}")
        ser = None # Set global ser to None
        return False # Indicate failure
    except Exception as e:
        error_msg = f"Error sending {action} command: {e}"
        write_log('error', error_msg)
        with state_lock:
            last_error = error_msg
        return False # Indicate failure

# --- Temperature Reading ---
def get_ssd_temp(sysfs_path):
    """Reads SSD temperature from the path specified in settings."""
    global last_error # Use state_lock for last_error

    if not sysfs_path or not isinstance(sysfs_path, str):
        error_msg = f"Invalid temperature path provided: {sysfs_path}"
        write_log('error', error_msg)
        with state_lock:
            # Don't overwrite serial/other errors with temp errors unless it's the only one, or specific to temp reading
            if last_error is None or "Temperature file" in last_error or "reading temperature" in last_error:
                last_error = error_msg
            elif "serial port" in last_error.lower(): # Preserve serial errors
                pass
            else: # Preserve other errors
                last_error = error_msg # Or append? Appending might be too verbose. Overwrite if generic.
        return None

    try:
        with open(sysfs_path, "r") as file:
            temp_millic = int(file.read().strip())
            temp_celsius = temp_millic / 1000.0
            # Don't clear last_error here on success, only on successful actions (like setting fan)
            # This allows temperature read errors to persist until a fan action succeeds or settings are updated
            return temp_celsius
    except FileNotFoundError:
        error_msg = f"Temperature file {sysfs_path} not found. Check path in settings."
        write_log('error', error_msg)
        with state_lock:
            if last_error is None or "Temperature file" in last_error:
                last_error = error_msg
            elif "serial port" in last_error.lower(): # Preserve serial errors
                pass
            else:
                last_error = error_msg
        return None
    except Exception as e:
        error_msg = f"Error reading temperature from {sysfs_path}: {e}"
        write_log('error', error_msg)
        with state_lock:
            if last_error is None or "reading temperature" in last_error:
                last_error = error_msg
            elif "serial port" in last_error.lower(): # Preserve serial errors
                pass
            else:
                last_error = error_msg
        return None

# Helper to find the state active at a specific timestamp based on history
def _get_state_at_time(timestamp, history_list, default_state):
    """
    Finds the fan state that was active at the given timestamp based on the history list.
    Assumes history_list is sorted chronologically.
    Returns default_state if history is empty or all events are after the timestamp.
    """
    state_at_time = default_state
    for ts, state in reversed(history_list):
        if ts <= timestamp:
            state_at_time = state
            break
    return state_at_time

# --- Fan Control Logic (Background Thread) ---
def fan_control_loop():
    """The main loop to check temperature and control the fan using current settings."""
    # Access globals needed in the loop
    global fan_state, current_temp, fan_history, last_error

    write_log('info', "Fan control thread started.")
    if not init_serial(): # Initial connection attempt using loaded settings
        write_log('warning', "Initial serial connection failed. Thread will retry.")

    # Get initial state based on loaded settings and temperature
    with settings_lock: # Read settings under lock
        local_ceiling = current_settings.get('threshold_ceiling', DEFAULT_SETTINGS['threshold_ceiling'])
        local_temp_path = current_settings.get("temp_path", DEFAULT_SETTINGS['temp_path'])
        local_history_duration = timedelta(hours=int(current_settings.get("history_duration_hours", DEFAULT_SETTINGS["history_duration_hours"])))

    initial_temp = get_ssd_temp(sysfs_path=local_temp_path)
    initial_state = False # Default assumption if temp is None
    if initial_temp is not None:
        with state_lock: # Lock for current_temp access
            current_temp = initial_temp
        # Determine initial state based on temperature vs ceiling threshold
        initial_state = initial_temp >= local_ceiling
        write_log('info', f"Initial Temp: {initial_temp:.1f}°C. Setting Initial Fan State: {'ON' if initial_state else 'OFF'}")
    else:
        write_log('warning', "Could not get initial temperature. Assuming fan OFF initially.")
        # current_temp is already None from get_ssd_temp call
        initial_state = False # Explicitly set to OFF if temp is unavailable

    # Record initial state and attempt to set it
    now = datetime.now()
    with state_lock: # Lock for shared state fan_state, fan_history
        # Ensure history starts with the determined initial state at the very beginning
        # We add an entry at `now`, the pruning logic below will handle keeping it within the window
        fan_history.append((now, initial_state))
        fan_state = initial_state
    set_fan(initial_state) # Attempt to set the physical fan state

    while not stop_thread.is_set():
        now = datetime.now()
        # Get current settings for this cycle under lock
        with settings_lock:
            local_ceiling = current_settings.get('threshold_ceiling', DEFAULT_SETTINGS['threshold_ceiling'])
            local_floor = current_settings.get('threshold_floor', DEFAULT_SETTINGS['threshold_floor'])
            local_temp_path = current_settings.get("temp_path", DEFAULT_SETTINGS['temp_path'])
            local_check_interval = current_settings.get("check_interval_seconds", DEFAULT_SETTINGS['check_interval_seconds'])
            local_history_duration = timedelta(hours=int(current_settings.get("history_duration_hours", DEFAULT_SETTINGS["history_duration_hours"])))

        # --- History Pruning ---
        # Always prune history based on the *maximum* configured duration
        cutoff_time = now - local_history_duration
        with state_lock: # Lock for history access
            while fan_history and fan_history[0][0] < cutoff_time:
                fan_history.popleft()
            # After pruning, ensure the earliest entry represents the state at or after cutoff_time
            # If history is now empty, the current state effectively covers the window.
            # If the earliest entry is > cutoff_time, the state from cutoff_time up to that point is implicitly the state of that first entry or current state if empty.
            # We might need to add a synthetic entry at `cutoff_time` if history doesn't start there?
            # Let's revisit this. The chart data logic is better equipped to handle the window start.
            # For the thread loop itself, just pruning is sufficient to keep deque size bounded.
            pass # Pruning logic moved below temp read


        temp = get_ssd_temp(sysfs_path=local_temp_path)

        # Update global current_temp under lock
        with state_lock:
            # Keep last known valid temp if current read fails, but update if successful
            current_temp = temp if temp is not None else current_temp
            # Prune history based on the *maximum* duration AFTER getting current time
            cutoff_time = now - local_history_duration
            while fan_history and fan_history[0][0] < cutoff_time:
                fan_history.popleft()


        if temp is None:
            write_log('warning', "Failed to get temperature reading. Skipping state check this cycle.")
            # Still need to wait using the interval from settings
            stop_thread.wait(local_check_interval)
            continue # Skip state change logic if temp read failed


        # State decision logic (using local copies of thresholds)
        with state_lock: # Read fan_state under lock for decision
            current_fan_state = fan_state

        new_state = current_fan_state # Assume no change initially

        if temp >= local_ceiling and not current_fan_state:
            write_log('info', f"Temp {temp:.1f}°C >= Ceiling {local_ceiling:.1f}°C. Turning fan ON.")
            new_state = True
        elif temp < local_floor and current_fan_state:
            write_log('info', f"Temp {temp:.1f}°C < Floor {local_floor:.1f}°C. Turning fan OFF.")
            new_state = False

        # Apply state change if needed
        if new_state != current_fan_state:
            if set_fan(new_state): # If command sent successfully
                with state_lock: # Update shared state under lock
                    fan_state = new_state
                    # Add the state change event to history
                    fan_history.append((now, new_state))
                    # Pruning happens at the start of the loop based on max duration
            else:
                write_log('error', "Failed to change fan state. State remains {}. Retrying next cycle.".format("ON" if current_fan_state else "OFF"))
                # Don't update fan_state or history if set_fan failed
        else:
            # If state is unchanged, add a history entry periodically to ensure graph
            # covers the full duration up to 'now' accurately, even with no changes.
            # Only add if the last history entry is significantly older than the interval.
            with state_lock:
                if not fan_history or (now - fan_history[-1][0]).total_seconds() > local_check_interval * 1.5: # Add point if last is old
                    fan_history.append((now, fan_state))
            pass # Pruning is handled at the start of the loop


        # Wait for the next check interval
        stop_thread.wait(local_check_interval)

    # --- Cleanup on thread exit ---
    write_log('info', "Fan control thread stopping.")
    if ser and ser.is_open:
        write_log('info', "Closing serial port.")
        # Optionally turn fan off on exit? set_fan(False) # Consider if desirable
        try:
            ser.close()
        except Exception as e:
            write_log('warning', f"Error during serial port close on exit: {e}")


# --- Flask Application ---
app = Flask(__name__)
app.secret_key = os.urandom(24) # Needed for flash messages

@app.route('/')
def index():
    """Renders the main control page with current status and settings."""
    with state_lock: # Lock for reading status variables
        template_status = {
            'current_temp': f"{current_temp:.1f}" if current_temp is not None else "N/A",
            'fan_state': "ON" if fan_state else "OFF",
            'last_error': last_error,
        }
    with settings_lock: # Lock for reading settings
        template_settings = {
            'threshold_ceiling': current_settings.get('threshold_ceiling'),
            'threshold_floor': current_settings.get('threshold_floor'),
            'command_open_hex': current_settings.get('command_open_hex'),
            'command_close_hex': current_settings.get('command_close_hex'),
            'temp_path': current_settings.get('temp_path'),
            'serial_port': current_settings.get('serial_port'), # Display monitored port
            'check_interval_seconds': current_settings.get('check_interval_seconds', DEFAULT_SETTINGS['check_interval_seconds']),
            'history_duration_hours': current_settings.get('history_duration_hours', DEFAULT_SETTINGS['history_duration_hours'])
        }

    return render_template('index.html', **template_status, **template_settings)

@app.route('/update_settings', methods=['POST'])
def update_settings():
    """Handles form submission to update settings and save to file."""
    global current_settings, history_duration, check_interval # We will modify these

    form_errors = []
    updated_settings = {}

    try:
        # Validate and collect thresholds
        new_ceiling = float(request.form['threshold_ceiling'])
        new_floor = float(request.form['threshold_floor'])
        if new_floor >= new_ceiling:
            form_errors.append("Floor threshold must be lower than ceiling threshold.")
        else:
            updated_settings['threshold_ceiling'] = new_ceiling
            updated_settings['threshold_floor'] = new_floor

        # Validate and collect commands (check hex format)
        open_hex = request.form['command_open_hex'].strip()
        close_hex = request.form['command_close_hex'].strip()

        # Temporarily derive bytes to check validity without updating globals yet
        temp_open_b, temp_close_b = derive_byte_commands(settings_dict={'command_open_hex': open_hex, 'command_close_hex': close_hex}, update_global=False)

        if temp_open_b is None: # derive_byte_commands logs the specific error
            form_errors.append(f"Invalid hex format for ON command: {open_hex}")
        else:
            updated_settings['command_open_hex'] = open_hex

        if temp_close_b is None: # derive_byte_commands logs the specific error
            form_errors.append(f"Invalid hex format for OFF command: {close_hex}")
        else:
            updated_settings['command_close_hex'] = close_hex


        # Collect temp path (basic validation: not empty)
        temp_path = request.form['temp_path'].strip()
        if not temp_path:
            form_errors.append("Temperature path cannot be empty.")
        else:
            updated_settings['temp_path'] = temp_path

        # Collect and validate history settings
        try:
            new_check_interval = int(request.form['check_interval_seconds'])
            if new_check_interval <= 0:
                form_errors.append("Check interval must be a positive integer.")
            else:
                updated_settings['check_interval_seconds'] = new_check_interval
        except ValueError:
            form_errors.append("Invalid number format for check interval.")

        try:
            new_history_duration_hours = int(request.form['history_duration_hours'])
            if new_history_duration_hours <= 0:
                form_errors.append("History duration must be a positive integer.")
            else:
                updated_settings['history_duration_hours'] = new_history_duration_hours
        except ValueError:
            form_errors.append("Invalid number format for history duration.")


    except ValueError as e:
        form_errors.append(f"Invalid number format in form data: {e}")
    except Exception as e:
        form_errors.append(f"An unexpected error occurred processing form data: {e}")
        write_log('error', f"Error processing settings form: {e}")

    if form_errors:
        for error in form_errors:
            flash(error, 'danger') # Use flash messages for errors
    else:
        # If no errors, update global settings and save
        with settings_lock:
            # Preserve existing settings not in the form (like serial_port, baud_rate if not in form)
            settings_to_save = current_settings.copy() # Start with current
            settings_to_save.update(updated_settings) # Apply validated updates

            # Ensure positive values for intervals/duration before saving
            settings_to_save['history_duration_hours'] = max(1, int(settings_to_save.get('history_duration_hours', DEFAULT_SETTINGS['history_duration_hours'])))
            settings_to_save['check_interval_seconds'] = max(1, int(settings_to_save.get('check_interval_seconds', DEFAULT_SETTINGS['check_interval_seconds'])))

            saved_ok = save_settings(settings_to_save) # Save the combined dict
            if saved_ok:
                # Update global runtime variables from the saved settings
                current_settings = settings_to_save.copy() # Update the global current_settings
                derive_byte_commands(update_global=True) # Update derived byte commands
                # Update other derived global settings
                history_duration = timedelta(hours=current_settings['history_duration_hours'])
                check_interval = current_settings['check_interval_seconds']

                flash('Settings updated successfully!', 'success')
                # Consider restarting serial if port changed (not currently in form)
                # Consider nudging the background thread about interval/duration change (it reads settings every cycle)
            else:
                flash('Settings updated in memory, but failed to save to file!', 'warning')


    return redirect(url_for('index')) # Redirect back to the main page

@app.route('/chart_data')
def chart_data():
    """API endpoint providing data for the history chart."""
    now = datetime.now()
    with settings_lock: # Get history duration from settings
        # Use the potentially updated history_duration from settings
        local_history_duration = timedelta(hours=int(current_settings.get("history_duration_hours", DEFAULT_SETTINGS["history_duration_hours"])))

    # Maximum allowed start time based on configured duration
    max_cutoff_time = now - local_history_duration

    with state_lock: # Lock access to fan_history and fan_state
        # Create a copy of the history that is within the max duration window *plus a small buffer*
        # This ensures we have the event just before max_cutoff_time if it exists
        history_copy_raw = list(fan_history) # Get the raw history deque content
        # The history_copy is used only for state calculation in the window, not for segments anymore.
        # We still need events that might be slightly before the window start to determine the state *at* the window start.
        # Let's keep the filtering for state calculation.
        history_copy = [item for item in history_copy_raw if item[0] >= max_cutoff_time - timedelta(seconds=current_settings.get("check_interval_seconds", DEFAULT_SETTINGS["check_interval_seconds"]))] # Filter within window + buffer

        current_fan_state_for_calc = fan_state # Get current state under lock
        current_temp_for_api = current_temp
        last_error_for_api = last_error

    # --- Determine the actual charting window start time ---
    # It's the later of: max_cutoff_time OR the timestamp of the earliest event in history_copy
    # If history_copy is empty, the effective start is 'now' (duration 0), or max_cutoff_time if we want to chart the full empty window
    # Based on the user's request ("真实统计到的时间数据"), if history is empty, the charted duration should be near zero,
    # OR if the app just started, show the current state extending back to max_cutoff_time.
    # Let's base the window start on the *earliest available history point* but not before max_cutoff_time.

    chart_window_start_time = max_cutoff_time # Default to max cutoff if no history within or before window

    if history_copy:
        # Find the timestamp of the very first event we have after pruning/filtering
        first_event_time = history_copy[0][0]
        chart_window_start_time = max(max_cutoff_time, first_event_time)
    # else: If history_copy is empty, chart_window_start_time remains max_cutoff_time.
    # This means if history is empty, the chart will show the current state over the full max duration window.
    # If that's not desired (user wants 0 duration for empty history), change this to `chart_window_start_time = now` if history_copy is empty.
    # Let's stick to showing current state over the requested max duration if history is empty, as it's less jarring than an empty chart.
    # If history is NOT empty, we respect the earliest history point.

    # Ensure window is valid (start time is before now)
    if chart_window_start_time > now:
        chart_window_start_time = now # Correct if clock moved backwards or very short window

    total_duration_for_charts = now - chart_window_start_time
    total_charted_seconds = total_duration_for_charts.total_seconds()

    # Handle cases where the duration is zero or negative
    if total_charted_seconds <= 0:
        chart_data = {
            'on_percentage': 100 if current_fan_state_for_calc else 0,
            'off_percentage': 100 if not current_fan_state_for_calc else 0,
            'total_on_seconds': 0,
            'total_off_seconds': 0,
            # 'history_segments': [], # Removed
            'total_charted_seconds': 0 # Report 0 total seconds
        }
        return jsonify(chart_data)

    # --- Calculate Pie Chart Data (Total ON/OFF time in the actual charting window) ---
    total_on_time = timedelta(0)
    total_off_time = timedelta(0)

    # Determine the state active *at* the beginning of the charting window
    state_at_chart_window_start = _get_state_at_time(chart_window_start_time, history_copy_raw, current_fan_state_for_calc) # Use raw history for state lookup

    current_interval_start_for_pie = chart_window_start_time
    current_state_in_interval_for_pie = state_at_chart_window_start

    # Iterate through history events that are *after* the window start
    for ts, state in history_copy:
        if ts > chart_window_start_time:
            # This event ends the current interval for calculation
            interval_end_time = ts
            duration = interval_end_time - current_interval_start_for_pie

            if duration > timedelta(0): # Ensure positive duration
                if current_state_in_interval_for_pie:
                    total_on_time += duration
                else:
                    total_off_time += duration

            # Start new interval
            current_interval_start_for_pie = interval_end_time
            current_state_in_interval_for_pie = state

    # Account for time from the last history event in the window (or window start) until 'now'
    duration_last_interval_for_pie = now - current_interval_start_for_pie
    if duration_last_interval_for_pie > timedelta(0):
        # The state for this final interval is the current state of the fan
        if current_fan_state_for_calc:
            total_on_time += duration_last_interval_for_pie
        else:
            total_off_time += duration_last_interval_for_pie


    on_percentage = (total_on_time.total_seconds() / total_charted_seconds) * 100
    off_percentage = 100 - on_percentage


    # --- Generate History Timeline Segments for Bar Chart ---
    # SEGMENT GENERATION LOGIC REMOVED

    chart_data = {
        'on_percentage': round(on_percentage, 1),
        'off_percentage': round(off_percentage, 1),
        'total_on_seconds': round(total_on_time.total_seconds()),
        'total_off_seconds': round(total_off_time.total_seconds()),
        # 'history_segments': segments, # Removed
        'total_charted_seconds': round(total_charted_seconds), # Add actual duration in seconds
        'current_temp': current_temp_for_api,
        'fan_state': "ON" if current_fan_state_for_calc else "OFF",
        'last_error': last_error_for_api
    }
    return jsonify(chart_data)


# --- Main Execution ---
def start_background_thread():
    """Starts the fan control background thread."""
    global control_thread
    write_log('info', "Starting fan control background thread.")
    stop_thread.clear()
    control_thread = threading.Thread(target=fan_control_loop, daemon=True)
    control_thread.start()

def stop_background_thread():
    """Signals the background thread to stop."""
    if control_thread and control_thread.is_alive():
        write_log('info',"Stopping fan control background thread.")
        stop_thread.set()
        # Calculate wait time based on settings to allow thread to check stop_thread.is_set()
        # Add a small buffer just in case.
        wait_interval = current_settings.get("check_interval_seconds", DEFAULT_SETTINGS['check_interval_seconds'])
        control_thread.join(timeout=wait_interval + 2) # Wait for thread to finish gracefully
        if control_thread.is_alive():
            write_log('warning',"Background thread did not stop gracefully. Forcing exit.")
            # In Python, there's no clean way to force terminate a thread.
            # Using daemon=True is the standard way to allow the main process to exit.


# Register cleanup function to stop the thread on exit
atexit.register(stop_background_thread)

if __name__ == '__main__':
    write_log('info', "--- Application Starting ---")
    # Ensure config directory exists before loading/saving
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # Load initial settings first
    load_settings()
    # Start the background task
    start_background_thread()
    # Run the Flask web server
    write_log('info', f"Starting Flask server on 0.0.0.0:{WEB_PORT}")
    # Use threaded=True for development/simple deployments if not using a WSGI server like Gunicorn
    # For production, a proper WSGI server is recommended.
    app.run(host='0.0.0.0', port=WEB_PORT, debug=False, threaded=True) # Use threaded=True
    write_log('info', "--- Application Stopping ---")