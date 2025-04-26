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
    "check_interval_seconds": 5,
    "history_duration_hours": 24 # Default history duration
}
# --- Persistent Configuration File Path (inside container) ---
CONFIG_DIR = "/config"
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, "settings.json")

# --- Web Server Port ---
WEB_PORT = 4812

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
        history_duration = timedelta(hours=int(current_settings.get("history_duration_hours", DEFAULT_SETTINGS["history_duration_hours"])))
        check_interval = int(current_settings.get("check_interval_seconds", DEFAULT_SETTINGS["check_interval_seconds"]))
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
        fan_history.append((now - local_history_duration - timedelta(seconds=1), initial_state)) # Add a state entry just before the history window
        fan_history.append((now, initial_state)) # Add the actual state at startup time
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


        temp = get_ssd_temp(sysfs_path=local_temp_path)

        # Update global current_temp under lock
        with state_lock:
            # Keep last known valid temp if current read fails, but update if successful
            current_temp = temp if temp is not None else current_temp
            # Prune history using duration from settings BEFORE potential append
            cutoff = now - local_history_duration
            while fan_history and fan_history[0][0] < cutoff:
                fan_history.popleft()


        if temp is None:
            write_log('warning', "Failed to get temperature reading. Skipping state check this cycle.")
            # Still need to wait and prune history based on 'now' and settings
            with state_lock:
                # Ensure history has at least the current state if it's empty after pruning
                if not fan_history:
                    fan_history.append((now, fan_state)) # Add current state at current time
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
                    fan_history.append((now, new_state)) # Record the state change event
                    # Pruning is now done at the beginning of the loop
            else:
                write_log('error', "Failed to change fan state. State remains {}. Retrying next cycle.".format("ON" if current_fan_state else "OFF"))
                # Don't update fan_state or history if set_fan failed
        else:
            # If state is unchanged, still ensure history reflects the current state point in time
            with state_lock:
                # This helps ensure the last segment in the chart data calculation goes up to 'now'
                # without needing complex logic to find the last state if no changes occurred recently.
                # We can append the current state periodically even if it hasn't changed,
                # or rely on the chart data logic to handle the final segment.
                # Appending every cycle might make the deque very large if interval is small.
                # Let's rely on the chart data logic for the final segment.
                pass # History pruning is handled at the start of the loop

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

            saved_ok = save_settings(settings_to_save) # Save the combined dict
            if saved_ok:
                # Update global runtime variables from the saved settings
                current_settings = settings_to_save.copy() # Update the global current_settings
                derive_byte_commands(update_global=True) # Update derived byte commands
                # Update other derived global settings
                history_duration = timedelta(hours=int(current_settings.get("history_duration_hours", DEFAULT_SETTINGS["history_duration_hours"])))
                check_interval = int(current_settings.get("check_interval_seconds", DEFAULT_SETTINGS["check_interval_seconds"]))

                flash('Settings updated successfully!', 'success')
                # Consider restarting serial if port changed (not currently in form)
                # Consider nudging the background thread about interval/duration change (it reads settings every cycle)
            else:
                flash('Settings updated in memory, but failed to save to file!', 'warning')


    return redirect(url_for('index')) # Redirect back to the main page

@app.route('/status')
def status():
    """API endpoint for current status (for potential JS updates)."""
    with state_lock:
        status_data = {
            'current_temp': current_temp,
            'fan_state': fan_state,
            'last_error': last_error
        }
    with settings_lock:
        # Also include settings in status if needed by JS
        status_data.update({
            'threshold_ceiling': current_settings.get('threshold_ceiling'),
            'threshold_floor': current_settings.get('threshold_floor'),
            'check_interval_seconds': current_settings.get('check_interval_seconds', DEFAULT_SETTINGS['check_interval_seconds']),
            'history_duration_hours': current_settings.get('history_duration_hours', DEFAULT_SETTINGS['history_duration_hours'])
        })
    return jsonify(status_data)

@app.route('/chart_data')
def chart_data():
    """API endpoint providing data for the history chart."""
    now = datetime.now()
    with settings_lock: # Get history duration from settings
        local_history_duration = timedelta(hours=int(current_settings.get("history_duration_hours", DEFAULT_SETTINGS["history_duration_hours"])))
    cutoff = now - local_history_duration

    total_on_time = timedelta(0)
    total_off_time = timedelta(0)

    segments = [] # List to hold history segments for the bar chart

    with state_lock: # Lock access to fan_history and fan_state
        history_copy = list(fan_history) # Work on a copy
        current_fan_state_for_calc = fan_state # Get current state under lock

    # --- Calculate Pie Chart Data (Total ON/OFF time in window) ---
    # This logic accounts for the state at the very beginning of the window
    last_time_for_pie = cutoff
    state_at_cutoff_for_pie = current_fan_state_for_calc # Assume current state extends back to cutoff

    # Find the last state entry at or before the cutoff time
    for ts, state in reversed(history_copy):
        if ts <= cutoff:
            state_at_cutoff_for_pie = state
            break
    # If no event found <= cutoff, state_at_cutoff_for_pie remains current_fan_state_for_calc

    current_state_for_pie_calc = state_at_cutoff_for_pie

    for ts, state in history_copy:
        if ts > cutoff: # Only consider events within or after the window start
            # Time duration from the last event (or cutoff) to this event
            duration = ts - max(last_time_for_pie, cutoff)
            if duration > timedelta(0):
                if current_state_for_pie_calc:
                    total_on_time += duration
                else:
                    total_off_time += duration

            last_time_for_pie = ts # Update last event time
            current_state_for_pie_calc = state # Update state for the next interval

    # Account for time from the last history event in the window until 'now'
    duration_since_last_event = now - max(last_time_for_pie, cutoff)
    if duration_since_last_event > timedelta(0):
        if current_fan_state_for_calc: # Use the actual current state
            total_on_time += duration_since_last_event
        else:
            total_off_time += duration_since_last_event

    total_duration_seconds = local_history_duration.total_seconds() # Total window duration in seconds
    on_percentage = (total_on_time.total_seconds() / total_duration_seconds) * 100 if total_duration_seconds > 0 else 0
    off_percentage = 100 - on_percentage

    # --- Generate History Timeline Segments for Bar Chart ---
    # Find the state at the start of the window (cutoff)
    state_at_cutoff_for_segments = current_fan_state_for_calc # Default assumption

    # Find the last event strictly before the cutoff time
    last_event_before_cutoff = None
    for ts, state in history_copy:
        if ts < cutoff:
            last_event_before_cutoff = (ts, state)
        else: # Events are now at or after cutoff, stop searching backwards
            break

    if last_event_before_cutoff:
        state_at_cutoff_for_segments = last_event_before_cutoff[1]
    # If no event is strictly before cutoff, state_at_cutoff_for_segments remains current_fan_state_for_calc


    current_segment_start_time = cutoff
    current_segment_state = state_at_cutoff_for_segments

    # Iterate through events that are within or after the window
    # We only care about events strictly *after* the cutoff to define segment boundaries
    for ts, state in history_copy:
        if ts > cutoff:
            # This event marks the end of the current segment and start of the next
            segment_end_time = ts
            duration = segment_end_time - current_segment_start_time

            # Add the completed segment if it has a positive duration
            if duration > timedelta(0):
                segments.append({
                    'state': 'ON' if current_segment_state else 'OFF',
                    'duration_seconds': duration.total_seconds()
                })

            # Start a new segment
            current_segment_start_time = segment_end_time
            current_segment_state = state

    # Add the final segment from the last event time within the window (or cutoff) to 'now'
    duration_last = now - current_segment_start_time
    if duration_last > timedelta(0):
        # The state of the final segment is the current state of the fan
        segments.append({
            'state': 'ON' if current_fan_state_for_calc else 'OFF',
            'duration_seconds': duration_last.total_seconds()
        })

    # Ensure segments are ordered by time (they should be based on history processing, but confirm)
    # The generated segments are already in chronological order.

    chart_data = {
        'on_percentage': round(on_percentage, 1),
        'off_percentage': round(off_percentage, 1),
        'total_on_seconds': round(total_on_time.total_seconds()),
        'total_off_seconds': round(total_off_time.total_seconds()),
        'history_segments': segments # Add the new data for the bar chart
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