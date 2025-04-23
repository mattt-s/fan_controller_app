import serial
import time
import threading
import atexit
import os

from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for
import logging
from collections import deque

# --- Configuration ---
# Serial Port (Adjust if needed)
SERIAL_PORT = '/dev/ttyUSB0'
BAUD_RATE = 9600

# Default Temperature Thresholds
DEFAULT_THRESHOLD_CEILING = 49.5
DEFAULT_THRESHOLD_FLOOR = 45.0

# Relay Commands (Adjust if needed)
COMMAND_OPEN = bytes.fromhex("A00101A2")
COMMAND_CLOSE = bytes.fromhex("A00100A1")

# SSD Temperature Path (Adjust if needed)
SYSFS_TEMP_PATH = "/sys/class/hwmon/hwmon0/temp1_input"

# Web Server Port
WEB_PORT = 4812

# Check Interval (seconds)
CHECK_INTERVAL = 5 # Increased interval for less frequent checks

# Chart History Duration
HISTORY_DURATION = timedelta(hours=24)

# --- Global Variables & Shared State ---
current_threshold_ceiling = DEFAULT_THRESHOLD_CEILING
current_threshold_floor = DEFAULT_THRESHOLD_FLOOR
fan_state = False  # False = Off, True = On
current_temp = None # Store the latest temperature reading
last_error = None # Store the last error message
ser = None # Serial object
control_thread = None
stop_thread = threading.Event()
fan_history = deque() # Store (timestamp, state) tuples
state_lock = threading.Lock() # To protect shared variables

# --- Logging Setup ---
logging.basicConfig(filename='fan.log',
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

# --- Serial Communication ---
def init_serial():
    """Initializes the serial connection."""
    global ser, last_error
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        write_log('info', f"Serial port {SERIAL_PORT} opened successfully.")
        last_error = None
        return True
    except serial.SerialException as e:
        error_msg = f"Cannot open serial port {SERIAL_PORT}: {e}"
        write_log('error', error_msg)
        last_error = error_msg
        ser = None # Ensure ser is None if connection failed
        return False
    except Exception as e:
        error_msg = f"Unexpected error opening serial port: {e}"
        write_log('error', error_msg)
        last_error = error_msg
        ser = None
        return False

def set_fan(state):
    """Controls the USB relay fan."""
    global ser, last_error
    if ser is None or not ser.is_open:
        write_log('warning', "Serial port not available. Cannot set fan state.")
        # Try to reconnect
        if not init_serial():
            # Persist error if reconnect fails
            last_error = f"Serial port {SERIAL_PORT} disconnected. Reconnect failed."
            return False # Indicate failure
        # If reconnect succeeds, continue

    command = COMMAND_OPEN if state else COMMAND_CLOSE
    action = "ON" if state else "OFF"
    try:
        ser.write(command)
        time.sleep(0.2) # Increased delay might be needed for some relays
        # Optional: Read response if your relay sends one
        # response = ser.readline().decode().strip()
        # if response:
        #     write_log('info', f"Device response to {action}: {response}")
        # else:
        #     write_log('info', f"Sent command to turn fan {action}. No response.")
        write_log('info', f"Sent command to turn fan {action}.")
        last_error = None # Clear error on success
        return True # Indicate success
    except serial.SerialException as e:
        error_msg = f"Serial error sending command: {e}"
        write_log('error', error_msg)
        last_error = error_msg
        # Close potentially broken port
        if ser:
            ser.close()
        ser = None
        return False # Indicate failure
    except Exception as e:
        error_msg = f"Error sending command: {e}"
        write_log('error', error_msg)
        last_error = error_msg
        return False # Indicate failure

# --- Temperature Reading ---
def get_ssd_temp(sysfs_path=SYSFS_TEMP_PATH):
    """Reads SSD temperature from sysfs."""
    global last_error
    try:
        with open(sysfs_path, "r") as file:
            temp_millic = int(file.read().strip())
            temp_celsius = temp_millic / 1000.0
            # last_error = None # Clear error on success - maybe not here, only clear on successful actions
            return temp_celsius
    except FileNotFoundError:
        error_msg = f"Temperature file {sysfs_path} not found. Check path."
        write_log('error', error_msg)
        # Don't overwrite serial errors with temp errors unless it's the only one
        if last_error is None or "Temperature file" not in last_error:
            last_error = error_msg
        return None
    except Exception as e:
        error_msg = f"Error reading temperature: {e}"
        write_log('error', error_msg)
        if last_error is None or "reading temperature" not in last_error:
            last_error = error_msg
        return None

# --- Fan Control Logic (Background Thread) ---
def fan_control_loop():
    """The main loop to check temperature and control the fan."""
    global fan_state, current_temp, fan_history, last_error

    write_log('info', "Fan control thread started.")
    # Try initial serial connection
    if not init_serial():
        write_log('warning', "Initial serial connection failed. Thread will retry.")
        # last_error is set by init_serial()

    # Initialize fan state based on initial temp (or default to off)
    initial_temp = get_ssd_temp()
    initial_state = False
    if initial_temp is not None:
        current_temp = initial_temp # Store initial temp
        if initial_temp >= current_threshold_ceiling:
            initial_state = True
        # Add initial state to history
        with state_lock:
            fan_history.append((datetime.now(), initial_state))
            fan_state = initial_state # Set global state
        write_log('info', f"Initial Temp: {initial_temp:.1f}°C. Initial Fan State: {'ON' if initial_state else 'OFF'}")
        set_fan(initial_state) # Attempt to set initial state
    else:
        write_log('warning', "Could not get initial temperature. Assuming fan OFF.")
        with state_lock:
            fan_history.append((datetime.now(), False)) # Record initial assumed OFF state
            fan_state = False # Set global state
        set_fan(False) # Attempt to turn off


    while not stop_thread.is_set():
        temp = get_ssd_temp()
        now = datetime.now()

        if temp is None:
            write_log('warning', "Failed to get temperature reading. Skipping cycle.")
            # Keep last known temp in current_temp
            time.sleep(CHECK_INTERVAL)
            continue # Skip the rest of the loop

        current_temp = temp # Update current temperature

        # Use local copies of thresholds inside the loop
        local_ceiling = current_threshold_ceiling
        local_floor = current_threshold_floor

        new_state = fan_state # Assume state doesn't change

        # Decide new state based on temperature and thresholds
        if temp >= local_ceiling and not fan_state:
            write_log('info', f"Temp {temp:.1f}°C >= Threshold {local_ceiling:.1f}°C. Turning fan ON.")
            new_state = True
        elif temp < local_floor and fan_state:
            write_log('info', f"Temp {temp:.1f}°C < Threshold {local_floor:.1f}°C. Turning fan OFF.")
            new_state = False

        # If state needs to change, attempt to set it and update history
        if new_state != fan_state:
            if set_fan(new_state): # If command sent successfully
                with state_lock:
                    fan_state = new_state # Update global state
                    fan_history.append((now, new_state)) # Record state change
                    # Prune old history
                    cutoff = now - HISTORY_DURATION
                    while fan_history and fan_history[0][0] < cutoff:
                        fan_history.popleft()
            else:
                # Error occurred and was logged by set_fan
                write_log('error', "Failed to change fan state. Retrying next cycle.")
                # Keep the old fan_state globally until successful change
        else:
            # If state is unchanged, ensure the latest state is recorded if history is empty
            with state_lock:
                if not fan_history:
                    fan_history.append((now, fan_state))
                # Still prune history even if state didn't change
                cutoff = now - HISTORY_DURATION
                while fan_history and fan_history[0][0] < cutoff:
                    fan_history.popleft()


        # Wait for the next check interval
        stop_thread.wait(CHECK_INTERVAL)

    # --- Cleanup on thread exit ---
    write_log('info', "Fan control thread stopping.")
    if ser and ser.is_open:
        write_log('info', "Closing serial port.")
        # Optionally turn fan off on exit? Depends on desired behavior.
        # set_fan(False)
        ser.close()

# --- Flask Application ---
app = Flask(__name__)
# Serve Chart.js locally
# app.static_folder = 'static'

@app.route('/')
def index():
    """Renders the main control page."""
    with state_lock:
        # Pass current state to the template
        template_data = {
            'current_temp': f"{current_temp:.1f}" if current_temp is not None else "N/A",
            'fan_state': "ON" if fan_state else "OFF",
            'threshold_ceiling': current_threshold_ceiling,
            'threshold_floor': current_threshold_floor,
            'last_error': last_error,
            'serial_port': SERIAL_PORT,
            'temp_path': SYSFS_TEMP_PATH
        }
    return render_template('index.html', **template_data)

@app.route('/update_settings', methods=['POST'])
def update_settings():
    """Handles form submission to update thresholds."""
    global current_threshold_ceiling, current_threshold_floor
    try:
        new_ceiling = float(request.form['threshold_ceiling'])
        new_floor = float(request.form['threshold_floor'])

        if new_floor >= new_ceiling:
            # Add flash message or handle error appropriately
            write_log('warning', "Invalid settings: Floor threshold must be lower than ceiling threshold.")
            # Keep old settings
        else:
            current_threshold_ceiling = new_ceiling
            current_threshold_floor = new_floor
            write_log('info', f"Settings updated: Ceiling={new_ceiling:.1f}°C, Floor={new_floor:.1f}°C")

    except ValueError:
        write_log('warning', "Invalid input for thresholds. Please enter numbers.")
    except Exception as e:
        write_log('error', f"Error updating settings: {e}")

    return redirect(url_for('index')) # Redirect back to the main page

@app.route('/status')
def status():
    """API endpoint for current status (for potential JS updates)."""
    with state_lock:
        status_data = {
            'current_temp': current_temp,
            'fan_state': fan_state,
            'threshold_ceiling': current_threshold_ceiling,
            'threshold_floor': current_threshold_floor,
            'last_error': last_error
        }
    return jsonify(status_data)

@app.route('/chart_data')
def chart_data():
    """API endpoint providing data for the history chart."""
    now = datetime.now()
    cutoff = now - HISTORY_DURATION
    total_on_time = timedelta(0)
    total_off_time = timedelta(0)
    last_time = cutoff # Start calculating from the beginning of the window

    processed_history = []
    with state_lock:
        # Find the state *at* the cutoff time
        initial_state_at_cutoff = False # Default assumption
        if fan_history:
            # Find the last state *before* or *at* the cutoff
            for ts, state in reversed(fan_history):
                if ts <= cutoff:
                    initial_state_at_cutoff = state
                    break
            else: # If no history before cutoff, use the earliest known state
                initial_state_at_cutoff = fan_history[0][1]

            last_state = initial_state_at_cutoff

            # Iterate through events within the window
            for ts, state in fan_history:
                if ts > cutoff:
                    duration = ts - last_time
                    if last_state: # If the fan was ON during this interval
                        total_on_time += duration
                    else: # Fan was OFF
                        total_off_time += duration
                    last_time = ts
                    last_state = state

            # Account for time from the last event until now
            duration_since_last = now - last_time
            if last_state:
                total_on_time += duration_since_last
            else:
                total_off_time += duration_since_last

        else: # No history at all
            # Assume current state prevailed for the whole duration? Or report 0?
            # Let's report 0 if no history exists in the window
            pass


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
    control_thread = threading.Thread(target=fan_control_loop, daemon=True)
    control_thread.start()

def stop_background_thread():
    """Signals the background thread to stop."""
    if control_thread and control_thread.is_alive():
        write_log('info',"Stopping fan control background thread.")
        stop_thread.set()
        control_thread.join(timeout=CHECK_INTERVAL + 2) # Wait for thread to finish
        if control_thread.is_alive():
            write_log('warning',"Background thread did not stop gracefully.")

# Register cleanup function to stop the thread on exit
atexit.register(stop_background_thread)

if __name__ == '__main__':
    write_log('info', "Starting Flask application.")
    if os.getenv("DEBUGPY") == "1":
        import debugpy
        debugpy.listen(("0.0.0.0", 5678))
        print("Waiting for debugger attach...")
        debugpy.wait_for_client()
    # Start the background task
    start_background_thread()
    # Run the Flask web server
    # Use host='0.0.0.0' to make it accessible outside the container
    app.run(host='0.0.0.0', port=WEB_PORT, debug=False) # Turn off debug mode for production/Docker