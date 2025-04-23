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
    "history_duration_hours": 24
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
fan_history = deque() # Store (timestamp, state) tuples
state_lock = threading.Lock() # Lock for fan_state, fan_history, current_temp, last_error
settings_lock = threading.Lock() # Separate lock for settings dictionary R/W
command_open_bytes = b'' # Derived from current_settings
command_close_bytes = b'' # Derived from current_settings
history_duration = timedelta(hours=DEFAULT_SETTINGS["history_duration_hours"]) # Derived
check_interval = DEFAULT_SETTINGS["check_interval_seconds"] # Derived


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
    try:
        if os.path.exists(CONFIG_FILE_PATH):
            with open(CONFIG_FILE_PATH, 'r') as f:
                loaded_settings = json.load(f)
            # Start with defaults and update with loaded values
            settings_temp = DEFAULT_SETTINGS.copy()
            settings_temp.update(loaded_settings)
            write_log('info', "Successfully loaded settings from file.")
            loaded_successfully = True
        else:
            write_log('warning', f"Config file not found. Using default settings.")
            settings_temp = DEFAULT_SETTINGS.copy()
            # Attempt to save defaults immediately
            save_settings(settings_temp)

    except (json.JSONDecodeError, IOError, Exception) as e:
        write_log('error', f"Error loading {CONFIG_FILE_PATH}: {e}. Using default settings.")
        settings_temp = DEFAULT_SETTINGS.copy()

    with settings_lock: # Lock before assigning to global
        current_settings = settings_temp
        # Derive dependent variables
        history_duration = timedelta(hours=current_settings.get("history_duration_hours", DEFAULT_SETTINGS["history_duration_hours"]))
        check_interval = current_settings.get("check_interval_seconds", DEFAULT_SETTINGS["check_interval_seconds"])
        # Derive byte commands safely
        derive_byte_commands(update_global=True) # Update globals command_open/close_bytes

    return loaded_successfully


def save_settings(settings_dict):
    """Saves the provided settings dictionary to the JSON file."""
    write_log('info', f"Attempting to save settings to {CONFIG_FILE_PATH}")
    try:
        # Ensure config directory exists
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE_PATH, 'w') as f:
            json.dump(settings_dict, f, indent=4)
        write_log('info', f"Settings successfully saved.")
        return True
    except (IOError, Exception) as e:
        write_log('error', f"Failed to save settings to {CONFIG_FILE_PATH}: {e}")
        # Optionally: Update last_error to show save failure in UI?
        # global last_error
        # with state_lock:
        #    last_error = f"Failed to save settings: {e}"
        return False

def derive_byte_commands(settings_dict=None, update_global=False):
    """
    Updates byte commands from hex strings.
    If settings_dict is provided, uses that dict. Otherwise uses global current_settings.
    If update_global is True, updates global command_open/close_bytes.
    Returns tuple (open_bytes, close_bytes) or (None, None) on error.
    """
    global command_open_bytes, command_close_bytes # Needed if update_global is True

    source_settings = settings_dict if settings_dict is not None else current_settings
    open_hex = source_settings.get("command_open_hex", DEFAULT_SETTINGS["command_open_hex"])
    close_hex = source_settings.get("command_close_hex", DEFAULT_SETTINGS["command_close_hex"])

    open_b = None
    close_b = None
    valid = True

    try:
        open_b = bytes.fromhex(open_hex)
    except (ValueError, TypeError):
        write_log('error', f"Invalid hex format for OPEN command: {open_hex}.")
        valid = False
        # Use default if invalid, but don't save it back here
        open_b = bytes.fromhex(DEFAULT_SETTINGS["command_open_hex"])


    try:
        close_b = bytes.fromhex(close_hex)
    except (ValueError, TypeError):
        write_log('error', f"Invalid hex format for CLOSE command: {close_hex}.")
        valid = False
        # Use default if invalid
        close_b = bytes.fromhex(DEFAULT_SETTINGS["command_close_hex"])

    if update_global:
        command_open_bytes = open_b
        command_close_bytes = close_b

    return (open_b, close_b) if valid else (None, None) # Indicate if original values were valid

# --- Serial Communication ---
def init_serial():
    """Initializes the serial connection based on current settings."""
    global ser, last_error
    port = current_settings.get("serial_port", DEFAULT_SETTINGS["serial_port"])
    rate = current_settings.get("baud_rate", DEFAULT_SETTINGS["baud_rate"])

    # Close existing connection if open
    if ser and ser.is_open:
        ser.close()
        write_log('info', f"Closed existing serial port connection.")
    ser = None # Reset serial object

    write_log('info', f"Attempting to open serial port {port} at {rate} baud.")
    try:
        ser = serial.Serial(port, rate, timeout=1)
        write_log('info', f"Serial port {port} opened successfully.")
        with state_lock: # Use state_lock for last_error consistency
            last_error = None
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
    global ser, last_error # Need global ser here too
    # Use state_lock for modifying last_error
    # Use settings_lock for reading command bytes (though they rarely change)

    with settings_lock: # Ensure we read the latest derived commands
        cmd_open = command_open_bytes
        cmd_close = command_close_bytes

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
        time.sleep(0.2) # Delay
        write_log('info', f"Sent command {command.hex()} to turn fan {action}.")
        with state_lock:
            last_error = None # Clear error on success
        return True # Indicate success
    except serial.SerialException as e:
        error_msg = f"Serial error sending {action} command: {e}"
        write_log('error', error_msg)
        with state_lock:
            last_error = error_msg
        # Close potentially broken port
        if ser:
            ser.close()
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
            # Don't overwrite serial errors with temp errors unless it's the only one
            if last_error is None or "Temperature file" not in last_error:
                last_error = error_msg
        return None

    try:
        with open(sysfs_path, "r") as file:
            temp_millic = int(file.read().strip())
            temp_celsius = temp_millic / 1000.0
            # Don't clear last_error here on success, only on successful actions
            return temp_celsius
    except FileNotFoundError:
        error_msg = f"Temperature file {sysfs_path} not found. Check path in settings."
        write_log('error', error_msg)
        with state_lock:
            if last_error is None or "Temperature file" not in last_error:
                last_error = error_msg
        return None
    except Exception as e:
        error_msg = f"Error reading temperature from {sysfs_path}: {e}"
        write_log('error', error_msg)
        with state_lock:
            if last_error is None or "reading temperature" not in last_error:
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

    # Get initial state based on loaded settings
    with settings_lock: # Read settings under lock
        local_ceiling = current_settings.get('threshold_ceiling', DEFAULT_SETTINGS['threshold_ceiling'])
        local_temp_path = current_settings.get("temp_path", DEFAULT_SETTINGS['temp_path'])

    initial_temp = get_ssd_temp(sysfs_path=local_temp_path)
    initial_state = False
    if initial_temp is not None:
        with state_lock: # Lock for current_temp access
            current_temp = initial_temp
        if initial_temp >= local_ceiling:
            initial_state = True
        write_log('info', f"Initial Temp: {initial_temp:.1f}°C. Setting Initial Fan State: {'ON' if initial_state else 'OFF'}")
    else:
        write_log('warning', "Could not get initial temperature. Assuming fan OFF.")
        initial_state = False

    # Record initial state and attempt to set it
    now = datetime.now()
    with state_lock: # Lock for shared state fan_state, fan_history
        fan_history.append((now, initial_state))
        fan_state = initial_state
    set_fan(initial_state)

    while not stop_thread.is_set():
        now = datetime.now()
        # Get current settings for this cycle under lock
        with settings_lock:
            local_ceiling = current_settings.get('threshold_ceiling', DEFAULT_SETTINGS['threshold_ceiling'])
            local_floor = current_settings.get('threshold_floor', DEFAULT_SETTINGS['threshold_floor'])
            local_temp_path = current_settings.get("temp_path", DEFAULT_SETTINGS['temp_path'])
            local_check_interval = current_settings.get("check_interval_seconds", DEFAULT_SETTINGS['check_interval_seconds'])
            local_history_duration = timedelta(hours=current_settings.get("history_duration_hours", DEFAULT_SETTINGS["history_duration_hours"]))


        temp = get_ssd_temp(sysfs_path=local_temp_path)

        # Update global current_temp under lock
        with state_lock:
            current_temp = temp if temp is not None else current_temp # Keep last known if error

        if temp is None:
            write_log('warning', "Failed to get temperature reading. Skipping cycle.")
            # Wait using the interval from settings
            stop_thread.wait(local_check_interval)
            continue

        # State decision logic (using local copies of thresholds)
        with state_lock: # Read fan_state under lock
            current_fan_state = fan_state

        new_state = current_fan_state # Assume no change

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
                    fan_history.append((now, new_state))
                    # Prune history using duration from settings
                    cutoff = now - local_history_duration
                    while fan_history and fan_history[0][0] < cutoff:
                        fan_history.popleft()
            else:
                write_log('error', "Failed to change fan state. State remains {}. Retrying next cycle.".format("ON" if current_fan_state else "OFF"))
                # Don't update fan_state or history if set_fan failed
        else:
            # If state is unchanged, still prune history
            with state_lock:
                if not fan_history: # Ensure history has at least the current state
                    fan_history.append((now, fan_state))
                cutoff = now - local_history_duration
                while fan_history and fan_history[0][0] < cutoff:
                    fan_history.popleft()

        # Wait for the next check interval
        stop_thread.wait(local_check_interval)

    # --- Cleanup on thread exit ---
    write_log('info', "Fan control thread stopping.")
    if ser and ser.is_open:
        write_log('info', "Closing serial port.")
        # Optionally turn fan off on exit? set_fan(False)
        ser.close()

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
        }

    return render_template('index.html', **template_status, **template_settings)

@app.route('/update_settings', methods=['POST'])
def update_settings():
    """Handles form submission to update settings and save to file."""
    global current_settings # We will modify this dict

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
        temp_path = request.form['temp_path'].strip()

        try:
            bytes.fromhex(open_hex)
            updated_settings['command_open_hex'] = open_hex
        except ValueError:
            form_errors.append(f"Invalid hex format for ON command: {open_hex}")

        try:
            bytes.fromhex(close_hex)
            updated_settings['command_close_hex'] = close_hex
        except ValueError:
            form_errors.append(f"Invalid hex format for OFF command: {close_hex}")

        # Collect temp path (basic validation: not empty)
        if not temp_path:
            form_errors.append("Temperature path cannot be empty.")
        else:
            updated_settings['temp_path'] = temp_path

        # Add other settings if they become configurable (e.g., serial port, interval)
        # updated_settings['serial_port'] = request.form['serial_port']
        # updated_settings['check_interval_seconds'] = int(request.form['check_interval_seconds'])


    except ValueError:
        form_errors.append("Invalid number format for thresholds.")
    except Exception as e:
        form_errors.append(f"An unexpected error occurred: {e}")
        write_log('error', f"Error processing settings form: {e}")

    if form_errors:
        for error in form_errors:
            flash(error, 'danger') # Use flash messages for errors
    else:
        # If no errors, update global settings and save
        with settings_lock:
            current_settings.update(updated_settings) # Update global dict
            saved_ok = save_settings(current_settings) # Save the whole dict
            if saved_ok:
                flash('Settings updated successfully!', 'success')
                # Update derived byte commands immediately
                derive_byte_commands(update_global=True)
                # Maybe re-init serial if port/baud changed?
                # init_serial() # Uncomment if port/baud become configurable
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
        })
    return jsonify(status_data)

@app.route('/chart_data')
def chart_data():
    """API endpoint providing data for the history chart."""
    now = datetime.now()
    with settings_lock: # Get history duration from settings
        local_history_duration = timedelta(hours=current_settings.get("history_duration_hours", DEFAULT_SETTINGS["history_duration_hours"]))
    cutoff = now - local_history_duration
    total_on_time = timedelta(0)
    total_off_time = timedelta(0)
    last_time = cutoff

    processed_history = []
    initial_state_at_cutoff = False # Default assumption

    with state_lock: # Lock access to fan_history and fan_state
        current_fan_state_for_calc = fan_state # Get current state under lock
        history_copy = list(fan_history) # Work on a copy

    if history_copy:
        # Find the state *at* the cutoff time
        for ts, state in reversed(history_copy):
            if ts <= cutoff:
                initial_state_at_cutoff = state
                break
        else: # If no history before cutoff, use the earliest known state
            initial_state_at_cutoff = history_copy[0][1]

        last_state = initial_state_at_cutoff

        # Iterate through events within the window
        for ts, state in history_copy:
            if ts > cutoff:
                # Cap start time at cutoff
                start_interval = max(last_time, cutoff)
                duration = ts - start_interval
                if duration > timedelta(0):
                    if last_state: # If the fan was ON during this interval
                        total_on_time += duration
                    else: # Fan was OFF
                        total_off_time += duration
                last_time = ts
                last_state = state

        # Account for time from the last event until now
        start_interval = max(last_time, cutoff)
        duration_since_last = now - start_interval
        if duration_since_last > timedelta(0):
            # Use the state active *now* (captured under lock earlier)
            if current_fan_state_for_calc:
                total_on_time += duration_since_last
            else:
                total_off_time += duration_since_last
    else: # No history at all in the window
        # If no history, assume current state for the whole duration
        duration_in_window = now - cutoff
        if current_fan_state_for_calc:
            total_on_time = duration_in_window
        else:
            total_off_time = duration_in_window

    total_duration = total_on_time + total_off_time
    on_percentage = (total_on_time.total_seconds() / total_duration.total_seconds()) * 100 if total_duration.total_seconds() > 0 else 0
    off_percentage = 100 - on_percentage

    chart_data = {
        'on_percentage': round(on_percentage, 1),
        'off_percentage': round(off_percentage, 1),
        'total_on_seconds': round(total_on_time.total_seconds()),
        'total_off_seconds': round(total_off_time.total_seconds())
    }
    return jsonify(chart_data)


# --- Main Execution ---
def start_background_thread():
    """Starts the fan control background thread."""
    global control_thread
    write_log('info', "Starting fan control background thread.")
    stop_thread.clear()
    # Pass necessary globals or make them accessible
    control_thread = threading.Thread(target=fan_control_loop, daemon=True)
    control_thread.start()

def stop_background_thread():
    """Signals the background thread to stop."""
    if control_thread and control_thread.is_alive():
        write_log('info',"Stopping fan control background thread.")
        stop_thread.set()
        # Calculate wait time based on settings
        wait_interval = current_settings.get("check_interval_seconds", DEFAULT_SETTINGS['check_interval_seconds'])
        control_thread.join(timeout=wait_interval + 2) # Wait for thread to finish
        if control_thread.is_alive():
            write_log('warning',"Background thread did not stop gracefully.")

# Register cleanup function to stop the thread on exit
atexit.register(stop_background_thread)

if __name__ == '__main__':
    write_log('info', "--- Application Starting ---")
    # Load initial settings first
    load_settings()
    # Start the background task
    start_background_thread()
    # Run the Flask web server
    write_log('info', f"Starting Flask server on 0.0.0.0:{WEB_PORT}")
    app.run(host='0.0.0.0', port=WEB_PORT, debug=False)
    write_log('info', "--- Application Stopping ---")