from smbus2 import SMBus
import time
import math

MPU_ADDR = 0x68
PWR_MGMT_1 = 0x6B
ACCEL_XOUT_H = 0x3B

# Initialize the bus
try:
    bus = SMBus(1)
    # Wake up the MPU6050
    bus.write_byte_data(MPU_ADDR, PWR_MGMT_1, 0)
    time.sleep(0.1)  # Stabilization delay
except OSError as e:
    print(f"--- HARDWARE ERROR: Could not find MPU6050 at 0x{MPU_ADDR:02X} ---")
    print("Check your I2C wiring (SDA/SCL) and ensure I2C is enabled in raspi-config.")
    # We don't exit here so the main script can handle the failure gracefully
    bus = None

def read_word(reg):
    if bus is None:
        return 0
    try:
        high = bus.read_byte_data(MPU_ADDR, reg)
        low = bus.read_byte_data(MPU_ADDR, reg + 1)
        value = (high << 8) + low
        if value >= 0x8000:
            value = -((65535 - value) + 1)
        return value
    except OSError:
        return 0

def get_accel():
    """Returns acceleration in Gs for x, y, and z axes."""
    # 16384.0 is the sensitivity scale factor for the default +/- 2g range
    ax = read_word(ACCEL_XOUT_H) / 16384.0
    ay = read_word(ACCEL_XOUT_H + 2) / 16384.0
    az = read_word(ACCEL_XOUT_H + 4) / 16384.0
    return {"x": ax, "y": ay, "z": az}

def magnitude(a):
    """Calculates the total acceleration vector length."""
    return math.sqrt(a["x"]**2 + a["y"]**2 + a["z"]**2)




            