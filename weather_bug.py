# Coded for CircuitPython 9+ on kb2040
# Ultra-Low-Power Hiking Weather Station: Light Sleep + Adaptive Polling + Zambretti

import time
import math
import json  
import board
import busio
import keypad
import analogio
import displayio
import terminalio
import adafruit_sht4x
import adafruit_bmp3xx
import digitalio
from adafruit_display_text import label
from i2cdisplaybus import I2CDisplayBus
import adafruit_displayio_ssd1306 as ssd1306

# Reinitializes display upon any soft reboot or hard reset
displayio.release_displays()


i2c = board.STEMMA_I2C() #built in STEMMA board

#creating DigitalInOut object on d3, analog on A0
vsys_adc = analogio.AnalogIn(board.A0)
batt_gnd_pin = digitalio.DigitalInOut(board.A3)
batt_gnd_pin.direction = digitalio.Direction.INPUT
BATTERY_TYPE = "LIPO"  # Options: "LIPO" (1S 3.3V-4.2V) or "AA" (3x AA 3.3V-4.5V)


# Initialize SHT4x sensor (0x44) and BMP390 pressure sensor (0x77 default, 0x76 alternate)
sht = adafruit_sht4x.SHT4x(i2c, 0x44)
sht.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION

try:
    bmp = adafruit_bmp3xx.BMP3XX_I2C(i2c, address=0x77)
except ValueError:
    bmp = adafruit_bmp3xx.BMP3XX_I2C(i2c, address=0x76)

# Balanced oversampling: ~0.25m resolution with ~50% lower power than 8x/2x
bmp.pressure_oversampling = 4
bmp.temperature_oversampling = 1



# Configure and initialize SSD1306 OLED display (128x64 at 0x3D)
ssd_width = 128
ssd_height = 64
ssd_bus = I2CDisplayBus(i2c, device_address=0x3D)
display = ssd1306.SSD1306(ssd_bus, width=ssd_width, height=ssd_height)
display.brightness = 0.25  # 25% brightness cuts OLED power draw by ~65%

# Button pins tuple (shared between keypad and PinAlarm light-sleep wakeups)
BUTTON_PINS = (board.D2, board.D4, board.D6, board.D8)

def init_keypad():
    return keypad.Keys(BUTTON_PINS, value_when_pressed=False, pull=True)

keys = init_keypad()

# 5-Row OLED Layout (21 chars max per line at scale=1)
TEXT_COLOR = 0xFFFFFF
rows = []
for y_pos in (0, 13, 26, 39, 52):
    lbl = label.Label(terminalio.FONT, anchor_point=(0.0, 0.0), anchored_position=(0, y_pos), scale=1, color=TEXT_COLOR)
    rows.append(lbl)

layer1 = displayio.Group()
for lbl in rows:
    layer1.append(lbl)
display.root_group = layer1

# Zambretti 26-state lookup table (<= 21 chars)
ZAMBRETTI_TABLE = (
    "Settled Fine",           # 1
    "Fine Weather",           # 2
    "Becoming Fine",          # 3
    "Fine, Less Settled",     # 4
    "Fine, Poss Showers",     # 5
    "Fairly Fine, Imprv",     # 6
    "Fairly Fine, Shwrs",     # 7
    "Fair, Showery Later",    # 8
    "Showery Early, Imprv",   # 9
    "Changeable, Mending",    # 10
    "Fairly Fine, Shwrs",     # 11
    "Unsettled, Clearing",    # 12
    "Unsettled, Prob Imprv",  # 13
    "Showery, Bright Int",    # 14
    "Showery, Unsettled",     # 15
    "Changeable, Some Rain",  # 16
    "Unsettled, Freq Rain",   # 17
    "Unsettled, Rain Later",  # 18
    "Unsettled, Some Rain",   # 19
    "Mostly Very Unsettled",  # 20
    "Occasional Rain, Wrse",  # 21
    "Rain at Times, Unsetl",  # 22
    "Rain at Freq Intvls",    # 23
    "Rain, Very Unsettled",   # 24
    "Stormy, May Improve",    # 25
    "Stormy, Much Rain",      # 26
)

LIPO_CURVE = (
    (4.20, 100), (4.15, 95), (4.11, 90), (4.08, 85), (4.02, 80),
    (3.98, 75),  (3.95, 70), (3.91, 65), (3.87, 60), (3.85, 55),
    (3.84, 50),  (3.82, 45), (3.80, 40), (3.79, 35), (3.77, 30),
    (3.75, 25),  (3.73, 20), (3.71, 15), (3.69, 10), (3.61, 5),
    (3.30, 0),
)

def read_battery():
    """switch gnd pin to output"""
    batt_gnd_pin.direction = digitalio.Direction.OUTPUT
    batt_gnd_pin.value = False
    try:
        time.sleep(0.002)
        raw_sum = 0
        for _ in range(4):
            raw_sum += vsys_adc.value
    finally:
        batt_gnd_pin.direction = digitalio.Direction.INPUT 

    voltage = ((raw_sum / 4.0) * 3.3 / 65535.0) * 2.0

    if voltage > 4.35 and BATTERY_TYPE == "LIPO":
        return voltage, 100, "USB"

    if BATTERY_TYPE == "AA":
        pct = int(round((voltage - 3.30) / (4.50 - 3.30) * 100.0))
        pct = max(0, min(100, pct))
        return voltage, pct, f"{pct}%"

    if voltage >= LIPO_CURVE[0][0]:
        return voltage, 100, "100%"
    if voltage <= LIPO_CURVE[-1][0]:
        return voltage, 0, "0%"

    for i in range(len(LIPO_CURVE) - 1):
        v_hi, p_hi = LIPO_CURVE[i]
        v_lo, p_lo = LIPO_CURVE[i + 1]
        if v_lo <= voltage <= v_hi:
            ratio = (voltage - v_lo) / (v_hi - v_lo)
            pct = int(round(p_lo + ratio * (p_hi - p_lo)))
            return voltage, pct, f"{pct}%"

    batt_gnd_pin.direction = digitalio.Direction.INPUT

    return voltage, 0, "0%"

def to_f(c):
    return c * 1.8 + 32.0

def calc_sea_level_pressure(station_hpa, temp_c, alt_m):
    if alt_m == 0:
        return station_hpa
    lapse = 0.0065 * alt_m
    denom = temp_c + lapse + 273.15
    if denom <= 0:
        return station_hpa
    return station_hpa * math.pow(1.0 - (lapse / denom), -5.257)

def calc_dew_point(temp_c, rh):
    rh_safe = max(1.0, min(100.0, rh))
    gamma = math.log(rh_safe / 100.0) + (17.625 * temp_c) / (243.04 + temp_c)
    return (243.04 * gamma) / (17.625 - gamma)

def run_zambretti(slp_hpa, delta_3h, temp_c, rh, is_summer=False):
    if delta_3h <= -1.6:
        z = round(127.0 - 0.12 * slp_hpa)
        if is_summer:
            z -= 1
        trend = "v"
    elif delta_3h >= 1.6:
        z = round(185.0 - 0.16 * slp_hpa)
        if is_summer:
            z += 1
        trend = "^"
    else:
        z = round(144.0 - 0.13 * slp_hpa)
        trend = "-"

    dew_spread = temp_c - calc_dew_point(temp_c, rh)
    if dew_spread < 2.0 and rh > 85.0:
        z += 2
    elif dew_spread > 10.0 and rh < 45.0:
        z -= 1

    z = max(1, min(26, int(z)))
    return ZAMBRETTI_TABLE[z - 1], trend

def adjust_altitude(current_alt_m, direction, fast_mode, fahrenheit):
    if fahrenheit:
        step_ft = 100.0 if fast_mode else 25.0
        step_m = step_ft / 3.28084
    else:
        step_m = 50.0 if fast_mode else 10.0
    return max(-100.0, min(6000.0, current_alt_m + (direction * step_m)))

def load_settings():
    defaults = {"altitude_m": 20.0, "use_fahrenheit": False, "is_summer": False, "timeout": 15.0}
    try:
        with open("/settings.json", "r") as f:
            data = json.load(f)
            defaults.update(data)
    except (OSError, ValueError):
        pass
    return defaults

def save_settings(alt_m, fahrenheit, summer, timeout_sec):
    try:
        with open("/settings.json", "w") as f:
            json.dump({
                "altitude_m": round(alt_m, 2),
                "use_fahrenheit": fahrenheit,
                "is_summer": summer,
                "timeout": timeout_sec,
            }, f)
        return "SETTINGS SAVED!"
    except OSError:
        return "ERR: USB READ-ONLY"

def light_sleep_until(wake_timestamp):
    """sleeps in shorrt chuncks until wake_timestamp OR any button press."""
    while time.monotonic() < wake_timestamp:
        if len(keys.events) > 0:
            return True
        time.sleep(0.05)
    return False
# Load saved preferences on boot
cfg = load_settings()
altitude_m = float(cfg["altitude_m"])
use_fahrenheit = bool(cfg["use_fahrenheit"])
is_summer = bool(cfg["is_summer"])
screen_timeout = float(cfg["timeout"])

# Sensor & tracking state
temperature = 20.0
relative_humidity = 50.0
wetbulb = 15.0
dew_point = 10.0
station_pressure = 1013.25
sea_level_pressure = 1013.25
batt_v, batt_pct, batt_tag = read_battery()

maxTemp = -100.0
minTemp = 100.0
maxHum = 0.0
minHum = 101.0
maxWet = -100.0
minWet = 101.0

# 3-hour rolling pressure history
HISTORY_INTERVAL = 900.0
MAX_HISTORY_SAMPLES = 12
pressure_history = []
last_history_log = -HISTORY_INTERVAL
delta_3h = 0.0

altitude_dirty = False
last_alt_change = 0.0
ALT_SETTLE_DELAY = 2.0

# Multi-page & button state
current_page = 0
TOTAL_PAGES = 3
screen_on = True

# Adaptive sampling intervals (saves massive power when screen is off in backpack)
ACTIVE_SENSOR_INTERVAL = 1.0    # 1s updates while viewing screen
SLEEP_SENSOR_INTERVAL = 15.0    # 15s updates while screen is asleep
BATTERY_INTERVAL = 60.0         # Sample battery every 60s
LONG_PRESS_TIME = 0.6

last_activity = time.monotonic()
last_sensor_read = -ACTIVE_SENSOR_INTERVAL
last_battery_read = time.monotonic()

press_times = [None, None, None, None]
held_alt_key = None
last_alt_repeat = 0.0
REPEAT_INITIAL_DELAY = 0.40
REPEAT_INTERVAL = 0.08
FAST_ACCEL_DELAY = 1.80

status_banner = ""
status_banner_until = 0.0
trend_symbol = "-"
forecast_text = "Initializing..."

def render_screen():
    if not screen_on:
        return

    if time.monotonic() < status_banner_until:
        rows[0].text = " "
        rows[1].text = "---------------------"
        rows[2].text = status_banner
        rows[3].text = "---------------------"
        rows[4].text = " "
        return

    unit = "F" if use_fahrenheit else "C"
    disp_t = to_f(temperature) if use_fahrenheit else temperature
    disp_min_t = to_f(minTemp) if use_fahrenheit else minTemp
    disp_max_t = to_f(maxTemp) if use_fahrenheit else maxTemp
    disp_w = to_f(wetbulb) if use_fahrenheit else wetbulb
    disp_min_w = to_f(minWet) if use_fahrenheit else minWet
    disp_max_w = to_f(maxWet) if use_fahrenheit else maxWet
    alt_str = f"{int(round(altitude_m * 3.28084))}ft" if use_fahrenheit else f"{int(round(altitude_m))}m"

    if current_page == 0:
        rows[0].text = f"{disp_min_t:0.0f}~{disp_t:0.1f}~{disp_max_t:0.0f}{unit} [{batt_tag}]"
        rows[1].text = f"{minHum:0.0f}~{relative_humidity:0.1f}~{maxHum:0.0f}% RH"
        rows[2].text = f"WB:{disp_min_w:0.0f}~{disp_w:0.1f}~{disp_max_w:0.0f}{unit}"
        t_char = "-" if altitude_dirty else trend_symbol
        rows[3].text = f"{sea_level_pressure:0.1f}hPa({t_char}) {alt_str}"
        rows[4].text = "Set Alt -> New Trend" if altitude_dirty else forecast_text

    elif current_page == 1:
        disp_dp = to_f(dew_point) if use_fahrenheit else dew_point
        spread = (disp_t - disp_dp)
        rows[0].text = f"ALTIMETER & AIR [{batt_tag}]"
        rows[1].text = f"Elev:{alt_str} (-/+ Alt)"
        rows[2].text = f"SLP:{sea_level_pressure:0.1f} Raw:{station_pressure:0.1f}"
        rows[3].text = f"Dew:{disp_dp:0.1f}{unit} Sprd:{spread:0.1f}"
        rows[4].text = f"3h dP:{delta_3h:+0.2f} [13:Sync]"

    elif current_page == 2:
        rows[0].text = "BATTERY & SETUP (3/3)"
        rows[1].text = f"Batt: {batt_v:0.2f}V ({batt_tag})"
        rows[2].text = f"[3] Units: {'Fahr (F)' if use_fahrenheit else 'Cels (C)'}"
        rows[3].text = f"[4] Season: {'Summer' if is_summer else 'Winter'}"
        t_str = "Off" if screen_timeout <= 0 else f"{int(screen_timeout)}s"
        rows[4].text = f"[5] Sleep:{t_str} (Hold:Sv)"

def show_banner(msg, duration=1.2):
    global status_banner, status_banner_until
    status_banner = msg
    status_banner_until = time.monotonic() + duration
    render_screen()

while True:
    now = time.monotonic()

    # 1. Process 4-Button Events
    event = keys.events.get()
    if event:
        k = event.key_number
        if event.pressed:
            press_times[k] = now
            last_activity = now

            if not screen_on:
                screen_on = True
                display.wake()
                # Force immediate sensor refresh upon waking screen
                last_sensor_read = -ACTIVE_SENSOR_INTERVAL
                render_screen()
                press_times[k] = None
                continue

            if k in (1, 2) and current_page in (0, 1):
                held_alt_key = k
                last_alt_repeat = now
                direction = -1 if k == 1 else 1
                altitude_m = adjust_altitude(altitude_m, direction, False, use_fahrenheit)
                altitude_dirty = True
                last_alt_change = now
                sea_level_pressure = calc_sea_level_pressure(station_pressure, temperature, altitude_m)
                render_screen()

        elif event.released and press_times[k] is not None:
            hold_dur = now - press_times[k]
            press_times[k] = None
            last_activity = now

            if k in (1, 2):
                held_alt_key = None

            if k == 0:  # GP14: Cycle Screen (Short) / Sleep Screen (Long)
                if hold_dur >= LONG_PRESS_TIME:
                    screen_on = False
                    display.sleep()
                else:
                    current_page = (current_page + 1) % TOTAL_PAGES
                    if current_page == 2:
                        batt_v, batt_pct, batt_tag = read_battery()
                    render_screen()

            elif k == 1 and current_page == 2:  # GP15: Units Toggle
                use_fahrenheit = not use_fahrenheit
                render_screen()

            elif k == 2 and current_page == 2:  # GP12: Season Toggle
                is_summer = not is_summer
                forecast_text, trend_symbol = run_zambretti(
                    sea_level_pressure, delta_3h, temperature, relative_humidity, is_summer
                )
                render_screen()

            elif k == 3:  # GP13: Save Settings (Long) / Page Action (Short)
                if hold_dur >= LONG_PRESS_TIME:
                    msg = save_settings(altitude_m, use_fahrenheit, is_summer, screen_timeout)
                    show_banner(msg, 1.5)
                else:
                    if current_page == 0:
                        maxTemp, minTemp = -100.0, 100.0
                        maxHum, minHum = 0.0, 101.0
                        maxWet, minWet = -100.0, 101.0
                        show_banner("MIN/MAX RESET", 1.0)
                    elif current_page == 1:
                        pressure_history = [sea_level_pressure]
                        last_history_log = now
                        delta_3h = 0.0
                        show_banner(f"SLP LOCKED:{sea_level_pressure:0.1f}", 1.2)
                    elif current_page == 2:
                        if screen_timeout == 15.0:
                            screen_timeout = 30.0
                        elif screen_timeout == 30.0:
                            screen_timeout = 0.0
                        else:
                            screen_timeout = 15.0
                        render_screen()

    # 2. Press-and-Hold Auto-Repeat for Altitude
    if held_alt_key is not None and current_page in (0, 1):
        held_for = now - (press_times[held_alt_key] or now)
        if held_for >= REPEAT_INITIAL_DELAY and (now - last_alt_repeat >= REPEAT_INTERVAL):
            last_alt_repeat = now
            last_activity = now
            last_alt_change = now
            altitude_dirty = True

            direction = -1 if held_alt_key == 1 else 1
            fast_mode = held_for >= FAST_ACCEL_DELAY
            altitude_m = adjust_altitude(altitude_m, direction, fast_mode, use_fahrenheit)
            sea_level_pressure = calc_sea_level_pressure(station_pressure, temperature, altitude_m)
            render_screen()

    # 3. Settle Altitude Baseline 2.0s after adjustment stops
    if altitude_dirty and (now - last_alt_change >= ALT_SETTLE_DELAY):
        altitude_dirty = False
        sea_level_pressure = calc_sea_level_pressure(station_pressure, temperature, altitude_m)
        pressure_history = [sea_level_pressure]
        last_history_log = now
        delta_3h = 0.0
        forecast_text, trend_symbol = run_zambretti(
            sea_level_pressure, delta_3h, temperature, relative_humidity, is_summer
        )
        render_screen()

    # 4. Automatic Screen Timeout
    if screen_on and screen_timeout > 0 and (now - last_activity >= screen_timeout):
        screen_on = False
        display.sleep()

    # 5. Adaptive Sensor & Battery Sampling
    current_interval = ACTIVE_SENSOR_INTERVAL if screen_on else SLEEP_SENSOR_INTERVAL
    if now - last_sensor_read >= current_interval:
        last_sensor_read = now

        temperature, relative_humidity = sht.measurements
        station_pressure = bmp.pressure
        sea_level_pressure = calc_sea_level_pressure(station_pressure, temperature, altitude_m)
        dew_point = calc_dew_point(temperature, relative_humidity)

        if now - last_battery_read >= BATTERY_INTERVAL:
            last_battery_read = now
            batt_v, batt_pct, batt_tag = read_battery()

        if not altitude_dirty:
            if now - last_history_log >= HISTORY_INTERVAL:
                last_history_log = now
                pressure_history.append(sea_level_pressure)
                if len(pressure_history) > MAX_HISTORY_SAMPLES:
                    pressure_history.pop(0)

            delta_3h = (sea_level_pressure - pressure_history[0]) if pressure_history else 0.0
            forecast_text, trend_symbol = run_zambretti(
                sea_level_pressure, delta_3h, temperature, relative_humidity, is_summer
            )

        # Stull wet-bulb temperature approximation (C)
        wetbulbA = math.sqrt(relative_humidity + 8.314)
        wetbulbB = 0.152 * wetbulbA
        wetbulbC = math.atan(wetbulbB)
        wetbulbD = temperature * wetbulbC
        wetbulbE = math.atan(0.023 * relative_humidity)
        wetbulbF = 0.004 * math.sqrt(relative_humidity**3)
        wetbulbG = wetbulbE * wetbulbF
        wetbulbH = math.atan(relative_humidity - 1.676)
        wetbulbJ = math.atan(temperature + relative_humidity)
        wetbulb = wetbulbD + wetbulbG - wetbulbH + wetbulbJ - 4.686

        maxTemp = max(temperature, maxTemp)
        minTemp = min(temperature, minTemp)
        maxHum = max(relative_humidity, maxHum)
        minHum = min(relative_humidity, minHum)
        maxWet = max(wetbulb, maxWet)
        minWet = min(wetbulb, minWet)

        if screen_on:
            render_screen()

    # 6. Power-Saving Sleep Scheduling
    # If any button is currently held down or altitude is settling, stay responsive with a short sleep
    if any(p is not None for p in press_times) or altitude_dirty:
        time.sleep(0.02)
    else:
        # Calculate exact timestamp of the next needed event
        next_sensor_due = last_sensor_read + (ACTIVE_SENSOR_INTERVAL if screen_on else SLEEP_SENSOR_INTERVAL)
        next_wake = next_sensor_due

        if screen_on and screen_timeout > 0:
            next_wake = min(next_wake, last_activity + screen_timeout)

        # Enter hardware light sleep until either a button is pressed or next_wake arrives
        woke_by_button = light_sleep_until(next_wake)
        if woke_by_button and not screen_on:
            # Wake screen immediately when a button wakes the Pico from deep/light sleep
            screen_on = True
            display.wake()
            last_activity = time.monotonic()
            last_sensor_read = -ACTIVE_SENSOR_INTERVAL
            render_screen()
# # SPDX-FileCopyrightText: 2023 DJDevon3
# # SPDX-License-Identifier: MIT
# # Pi Pico & Picowbell with SSD1306 display & BME280 sensor
# # Coded for Circuit Python 8.1
# 
# import time  
# import digitalio
# import board
# import busio
# import displayio
# import terminalio
# import adafruit_sht4x
# import math
# from adafruit_display_text import label
# from i2cdisplaybus import I2CDisplayBus
# 
# import adafruit_displayio_ssd1306 as ssd1306
# ##Screen on/off switch coding
# screenSwitch = digitalio.DigitalInOut(board.GP14) # set pin
# screenSwitch.switch_to_input(pull=digitalio.Pull.UP) # configure switch
# #Reset Switch coding
# resetSwitch = digitalio.DigitalInOut(board.GP15) # set pin
# resetSwitch.switch_to_input(pull=digitalio.Pull.UP) # configure switch
# 
# 
# # Reinitalizes display upon any soft reboot or hard reset
# displayio.release_displays()
# 
# # Pi Pico RP2040 I2C0 bus initialization (SSD1306 display)
# ##i2c0 = busio.I2C(board.GP3, board.GP2)
# i2c = busio.I2C(scl=board.GP5,sda = board.GP4)
# # Pi Pico RP2040 I2C1 bus initialization (temp sensor from Stemma port)
# ##i2c1 = busio.I2C(board.GP5, board.GP4)
# ##i2c2 = busio.I2C(scl=board.GP5,sda = board.GP4) 
# # i2c = board.I2C()  # other boards use board.SCL and board.SDA
# 
# # Initialize BME280 sensor
# ##bme280 = adafruit_bme280.Adafruit_BME280_I2C(i2c1)
# sht = adafruit_sht4x.SHT4x(i2c, 0x44)
# sht.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION
# 
# # Configure display size
# ssd_width = 128
# ssd_height = 32	
# 
# # Ensure the physical address of your SSD1306 is set here:
# ssd_bus = I2CDisplayBus(i2c, device_address=0x3C)
# display = ssd1306.SSD1306(ssd_bus, width=ssd_width, height=ssd_height)
# 
# # Manually set your sea_level_pressure to your area
# # This can change houly, lookup NOAA data or automate it.
# # On a normal sunny day it's generally 1013-1015
# ##bme280.sea_level_pressure = 1013.4
# # If you live at 0 sea level this is a nice workaround
# # bme280.sea_level_pressure = bme280.pressure
# 
# # Quick Colors for Labels
# 
# TEXT_COLOR = 0xFFFFFF
# 
# temp_label = label.Label(terminalio.FONT)
# temp_label.anchor_point = (0.0, 0.0)
# temp_label.anchored_position = (0, 0)
# temp_label.scale = 1
# temp_label.color = TEXT_COLOR
# 
# humidity_label = label.Label(terminalio.FONT)
# humidity_label.anchor_point = (0.0, 0.0)
# humidity_label.anchored_position = (0, 10)
# humidity_label.scale = 1
# humidity_label.color = TEXT_COLOR
# 
# wetbulb_label = label.Label(terminalio.FONT)
# wetbulb_label.anchor_point = (0.0, 0.0)
# wetbulb_label.anchored_position = (0, 20)
# wetbulb_label.scale = 1
# wetbulb_label.color = TEXT_COLOR
# 
# maxmin_label = label.Label(terminalio.FONT)
# maxmin_label.anchor_point = (0.0, 0.0)
# maxmin_label.anchored_position = (0, 30)
# maxmin_label.scale = 1
# maxmin_label.color = TEXT_COLOR
# 
# 
# # Create DisplayIO Group Layer
# layer1 = displayio.Group()
# layer1.append(temp_label)
# layer1.append(humidity_label)
# layer1.append(wetbulb_label)
# display.root_group = layer1
# 
# prevTemp = float('-inf')
# maxTemp = float(1)
# minTemp = float(32)
# maxHum = float(1) 
# minHum = float(101)
# maxWet = float(1)
# minWet = float(100)
# latchVal = bool(True)
# 
# while True:
#     # Convert temp C to F
#     ##temperature = f"{bme280.temperature * 1.8 + 32:.1f}"
#   # Displays labels on the SSD1306 display
#     temperature, relative_humidity = sht.measurements
#     wetbulbA = math.sqrt(relative_humidity + 8.314)
#     wetbulbB = 0.152 * wetbulbA
#     wetbulbC = math.atan(wetbulbB)
#     wetbulbD = temperature * wetbulbC
#     wetbulbE = math.atan(0.023 * relative_humidity)   
#     wetbulbF = 0.004 * math.sqrt(relative_humidity**3)
#     wetbulbG = wetbulbE * wetbulbF
#     wetbulbH = math.atan(relative_humidity - 1.676)
#     wetbulbJ = math.atan(temperature + relative_humidity)
#     wetbulb = wetbulbD + wetbulbG - wetbulbH + wetbulbJ - 4.686
#     
#     maxTemp = max(temperature, maxTemp)
#     minTemp = min(temperature, minTemp)
#     
#     maxHum = max(relative_humidity, maxHum)
#     minHum = min(relative_humidity, minHum)
#     
#     maxWet = max(wetbulb, maxWet)
#     minWet = min(wetbulb, minWet)
#     
#     if screenSwitch.value == False:
#         if latchVal == True:
#             latchVal = False
#         else:
#             latchVal = True
# 
# 
#     
#     if latchVal == True:
# 
#         display.sleep()
# 
# 
#     else:
#         display.wake()
#         TEXT_COLOR = 0xFFFFFF
#         maxmin_label.color = TEXT_COLOR
#         wetbulb_label.color = TEXT_COLOR
#         humidity_label.color = TEXT_COLOR
#         temp_label.color = TEXT_COLOR
#         
#         temp_label.text = f"{minTemp:0.1f} ~ {temperature:0.1f} ~ {maxTemp:0.1f} C"
#         humidity_label.text = f"{minHum:0.1f} ~ {relative_humidity:0.1f} ~ {maxHum:0.1f} %"
#         wetbulb_label.text = f"{minWet:0.1f} ~ {wetbulb:0.1f} ~ {maxWet:0.1f} C"
#         
#         
#     if resetSwitch.value == False:
#         display.wake  ()
#         maxTemp = 0
#         minTemp = 101
#         maxHum = 0
#         minHum = 101
#         maxWet = 0
#         minWet = 101
#         TEXT_COLOR = 0xFFFFFF
#         temp_label.text = " "
#         humidity_label.text = "RESET"
#         wetbulb_label.text = " "
#         
#         maxmin_label.color = TEXT_COLOR
#         wetbulb_label.color = TEXT_COLOR
#         humidity_label.color = TEXT_COLOR
#         temp_label.color = TEXT_COLOR
# 
# 
# 
#     
# 
#     # Prints Serial Data to REPL console (good for debugging)
#     print("Temperature: %0.1f %%" % sht.temperature)
#     print("Humidity: %0.1f %%" % sht.relative_humidity)
#     print("Wet Bulb: %0.1f %%" % wetbulb)
#     print("Max Temperature: %0.1f %%" % maxTemp)
#     print("Min Temperature: %0.1f %%" % minTemp)
#     print(screenSwitch.value)
#     print(resetSwitch.value)
#     print(latchVal)
# 
# #     print("Altitude = %0.2f meters" % bme280.altitude)
#     time.sleep(0.1)
