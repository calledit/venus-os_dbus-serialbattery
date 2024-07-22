# -*- coding: utf-8 -*-

# NOTES
# Please see "Add/Request a new BMS" https://mr-manuel.github.io/venus-os_dbus-serialbattery/general/supported-bms#add-by-opening-a-pull-request
# in the documentation for a checklist what you have to do, when adding a new BMS

# avoid importing wildcards, remove unused imports
from battery import Battery, Cell
from utils import is_bit_set, read_serial_data, logger
import utils
from struct import unpack_from
import threading
import asyncio
import time
from bleak import BleakClient

import sys


class LiTime_Ble(Battery):
    def __init__(self, port, baud, address):
        super(LiTime_Ble, self).__init__(port, baud, address)
        self.type = self.BATTERYTYPE
        self.address = address
        #self.poll_interval = 5000
        self.poll_interval = 2000

    BATTERYTYPE = "Litime"

    write_characteristic = "0000ffe2-0000-1000-8000-00805f9b34fb"
    read_characteristic = "0000ffe1-0000-1000-8000-00805f9b34fb"

    query_battery_status = bytes([0x00, 0x00, 0x04, 0x01, 0x13, 0x55, 0xAA, 0x17])

    ble_async_thread_ready = threading.Event()
    ble_connection_ready = threading.Event()
    ble_async_thread_event_loop = False
    client = False
    response_event = False
    response_data = False
    main_thred = False

    Last_remianAh = 0
    Last_remianAh_time = 0
    Last_remianAh_initiation = 0
    current_based_on_remaning = 0
    last_few_currents = []


    def test_connection(self):
        """
        call a function that will connect to the battery, send a command and retrieve the result.
        The result or call should be unique to this BMS. Battery name or version, etc.
        Return True if success, False for failure
        """
        logger.info("test_connection")
        self.main_thred = threading.current_thread()
        ble_async_thread = threading.Thread(name="BMS_bluetooth_async_thred", target=self.initiate_ble_thread_main, daemon=True)
        ble_async_thread.start()
        thread_start_ok = self.ble_async_thread_ready.wait(2)
        connected_ok = self.ble_connection_ready.wait(90)
        if not thread_start_ok:
            logger.error("thread took to long to start")
            return False
        if not connected_ok:
            logger.error("Chnage BLE connection to BMS took to long to inititate")
            return False

        self.send_com()

        return True

        result = False
        try:
            result = self.read_status_data()
            # get first data to show in startup log, only if result is true
            result = result and self.refresh_data()
        except Exception:
            (
                exception_type,
                exception_object,
                exception_traceback,
            ) = sys.exc_info()
            file = exception_traceback.tb_frame.f_code.co_filename
            line = exception_traceback.tb_lineno
            logger.error(
                f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}"
            )
            result = False

        return result

    def client_disconnected(self, client):
        logger.info("client_disconnected(client)")

    #saves response and tells the command sender that the response has arived
    def notify_read_callback(self, sender, data: bytearray):
        self.response_data = data
        self.response_event.set()



    def initiate_ble_thread_main(self):
        asyncio.run(self.async_main(self.address))

    async def async_main(self, address):
        self.ble_async_thread_event_loop = asyncio.get_event_loop()
        self.ble_async_thread_ready.set()

        #try to connect over and over if the connection fails
        while self.main_thred.is_alive():
            await self.connect_to_bms(self.address)
            await asyncio.sleep(1)#sleep one second before trying to reconnecting

    async def connect_to_bms(self, address):
        logger.info("create client: "+address)
        self.client = BleakClient(address, disconnected_callback=self.client_disconnected)
        try:
            logger.info("connect: "+address)
            await self.client.connect()
            logger.info("connected")
            await self.client.start_notify(self.read_characteristic, self.notify_read_callback)
            logger.info("notifications active")

        except Exception as e:
            logger.error("Failed when trying to connect", e)
            #logger.error(e)
            return False
        finally:
            self.ble_connection_ready.set()
            while self.client.is_connected and self.main_thred.is_alive():
                await asyncio.sleep(0.1)
            await self.client.disconnect()


    async def ble_thred_send_com(self, command):
        self.response_event = asyncio.Event()
        self.response_data = False
        await self.client.write_gatt_char(self.write_characteristic, command, True)
        await asyncio.wait_for(self.response_event.wait(), timeout=1)#Wait for the response notification
        self.response_event = False
        return self.response_data

    async def send_corutine_to_ble_thread_and_wait_for_result(self, corutine):
        bt_task = asyncio.run_coroutine_threadsafe(corutine, self.ble_async_thread_event_loop)
        result = await asyncio.wait_for(asyncio.wrap_future(bt_task), timeout=1.5)
        return result

    def send_com(self):
        #logger.info("requesting battery status")
        data = asyncio.run(self.send_corutine_to_ble_thread_and_wait_for_result(self.ble_thred_send_com(self.query_battery_status)))
        #logger.info("got battery status")
        #print("\n")
        self.parse_status(data)

    def unique_identifier(self) -> str:
        """
        Used to identify a BMS when multiple BMS are connected
        Provide a unique identifier from the BMS to identify a BMS, if multiple same BMS are connected
        e.g. the serial number
        If there is no such value, please remove this function
        """
        #logger.info("unique_identifier called before get_settings so serial_number is not populated")
        #return "litime_serial_12345"

        return self.address

    def connection_name(self) -> str:
      return "BLE " + self.address

    def custom_name(self) -> str:
        return "Bat: " + self.type + " " + self.address[-5:]

    def parse_status(self, data):
        #voltages 8-16
        messured_total_voltage, cells_added_together_voltage = unpack_from("II", data, 8)
        messured_total_voltage /= 1000
        cells_added_together_voltage /= 1000

        heat, not_known4, protection_state, failure_state, is_balancing, battery_state, SOC, SOH, discharges_count, discharges_amph_count = unpack_from("IIIIIHHIII", data, 68)

        #Cell voltages 16-48
        nr_of_cells = 0
        cellv_str = ""
        for byte_pos in range(16, 48, 2):
            cell_volt, = unpack_from("H", data, byte_pos)
            if cell_volt != 0:
                if len(self.cells) >= nr_of_cells:
                    self.cells.append(Cell(False))
                cell_volt = cell_volt/1000
                self.cells[nr_of_cells].voltage = cell_volt
                self.cells[nr_of_cells].balance = (is_balancing & pow(2, nr_of_cells)) != 0
                cellv_str += str(cell_volt)+","
                nr_of_cells += 1

        self.cell_count = nr_of_cells

        self.max_battery_voltage = utils.MAX_CELL_VOLTAGE * self.cell_count
        self.min_battery_voltage = utils.MIN_CELL_VOLTAGE * self.cell_count

        #print("cell_voltages: ", cell_voltages)

        #Curent, temp and so on 48-68
        #c1, c2, c3, c4 = unpack_from("BBBB", data, 48)
        #cur1, cur2 = unpack_from("hh", data, 48)
        current, cell_temp, mosfet_temp, unknown_temp, not_known1, not_known2, remaining_amph, full_charge_capacity_amph, not_known3 = unpack_from("ihhhHHHHH", data, 48)
        #current seams wrong
        current = current/1000

        remaining_amph /= 100
        full_charge_capacity_amph /= 100


        #print(f"current: {current}, cell_temp: {cell_temp}, mosfet_temp: {mosfet_temp}, unknown_temp: {unknown_temp}, not_known1: {not_known1}, not_known2: {not_known2}")
        #print(f"remaining_amph: {remaining_amph}, full_charge_capacity_amph: {full_charge_capacity_amph}, not_known3: {not_known3}")

        B1, B2, B3, B4 = unpack_from("BBBB", data, 84)
        #logger.info(f"bal bytes: {B1}, {B2}, {B3}, {B4}")
        H1, H2, H3, H4 = unpack_from("BBBB", data, 68)
        #logger.info(f"heat bytes: {H1}, {H2}, {H3}, {H4}")

        #print(f"heat: {heat}, not_known4: {not_known4}, protection_state: {protection_state}, failure_state: {failure_state}, is_balancing: {is_balancing}, battery_state: {battery_state}, SOC: {SOC}, SOH: {SOH}, discharges_count: {discharges_count}, discharges_amph_count: {discharges_amph_count}")

        self.capacity = full_charge_capacity_amph
        self.voltage = messured_total_voltage
        self.soc = SOC

        if is_balancing != 0:
             self.balance_fet = True
        else:
             self.balance_fet = False

        f = open("/data/charge_log.txt", "a")
        timestr = time.ctime()
        f.write(f"timestr: {timestr} curr: {current}, nk1: {not_known1}, nk2: {not_known2}, n3: {not_known3},  SOC: {SOC}, tot_v: {messured_total_voltage}, add_v: {cells_added_together_voltage}, protect_state: {protection_state}, fail_state: {failure_state}, is_bal: {bin(is_balancing)}, bat_st: {battery_state}, heat: {heat}, nk4: {not_known4}, rem_ah: {remaining_amph} {cellv_str}\n")
        f.close()

        current_based_on_remaning = 0

        if self.Last_remianAh == 0:
            self.current = 0
            self.Last_remianAh = remaining_amph
            self.Last_remianAh_time = time.time()
            logger.info("initate Last_remianAh")

        time_since_last_update = int(time.time() - self.Last_remianAh_time)
        if self.Last_remianAh != remaining_amph:
            now_time = time.time()
            Last_remianAh_time_diff = float(now_time - self.Last_remianAh_time)/3600
            Last_remianAh_change_diff = remaining_amph - self.Last_remianAh
            self.Last_remianAh = remaining_amph
            self.Last_remianAh_time = now_time
            if self.Last_remianAh_initiation == 0:
                self.Last_remianAh_initiation = 1
            else:
                self.current_based_on_remaning = Last_remianAh_change_diff/Last_remianAh_time_diff
                #logger.info(f"update current_based_on_remaning: {self.current_based_on_remaning} current: {current}")
                self.Last_remianAh_initiation = 2
        #else:
        #    logger.info(f"unchanged current_based_on_remaning: {self.current_based_on_remaning} current: {current}")

        self.last_few_currents.append(current)
        if len(self.last_few_currents) > 5:
            self.last_few_currents.pop(0)

        Last_few_avg = sum(self.last_few_currents)/len(self.last_few_currents)

        Use_Reason = ""
        #if last update was long ago we use the current reported by the bms despite it beeing unstable, we also use the current from the BMS if there is a very large discrepency betwen them
        if time_since_last_update > 120:
            self.current = Last_few_avg
            Use_Reason = "curr: over 120s since last remaining_amph update"

        elif self.Last_remianAh_initiation != 2:
            self.current = Last_few_avg
            Use_Reason = "curr: Last_remianAh not initiated with base values"

        elif time_since_last_update > 5 and (self.current_based_on_remaning + 3 < self.current or self.current_based_on_remaning - 3 > self.current):
            self.current = Last_few_avg
            Use_Reason = "curr: Large differances betwen base and curr despite recent base update"

        else:
            self.current = self.current_based_on_remaning
            Use_Reason = "base"

        logger.info(f"{Use_Reason}, current:{current:.3f}, Last_few_avg: {Last_few_avg:.3f}, base: {self.current_based_on_remaning:.3f}")#, cur1: {cur1}, cur2: {cur2}, c1: {c1}, c2: {c2}, c3: {c3}, c4: {c4}")

        #Overwrite earlier logic
        #self.current = current

        # temperature sensor 1 in °C (float)
        temp1 = cell_temp
        self.to_temp(1, temp1)

        # status of the battery if charging is enabled (bool)
        self.charge_fet = True
        if battery_state == 4:
            self.charge_fet = False

        # status of the battery if discharging is enabled (bool)
        self.discharge_fet = True

        self.capacity_remaining = remaining_amph

        # temperature sensor 2 in °C (float)
        temp2 = unknown_temp
        self.to_temp(2, temp2)

        # temperature sensor MOSFET in °C (float)
        temp_mos = mosfet_temp
        self.to_temp(0, temp_mos)

        self.history.total_ah_drawn = discharges_amph_count

        self.history.full_discharges = discharges_count

    def get_settings(self):
        """
        After successful connection get_settings() will be called to set up the battery
        Set all values that only need to be set once
        Return True if success, False for failure
        """
        logger.info("get_settings")


        # MANDATORY values to set
        # does not need to be in this function, but has to be set at least once
        # could also be read in a function that is called from refresh_data()
        #
        # if not available from battery, then add a section in the `config.default.ini`
        # under ; --------- BMS specific settings ---------

        # number of connected cells (int)
        #self.cell_count = 8 #VALUE_FROM_BMS

        # capacity of the battery in ampere hours (float)
        #self.capacity = 100 #VALUE_FROM_BMS

        # init the cell array once XXXXX FIX 8 hardcoded
        for _ in range(8):
            self.cells.append(Cell(False))


        self.send_com()

        self.max_battery_voltage = utils.MAX_CELL_VOLTAGE * self.cell_count
        self.min_battery_voltage = utils.MIN_CELL_VOLTAGE * self.cell_count

        # OPTIONAL values to set
        # does not need to be in this function
        # could also be read in a function that is called from refresh_data()
        # maximum charge current in amps (float)
        #self.max_battery_charge_current = VALUE_FROM_BMS

        # maximum discharge current in amps (float)
        #self.max_battery_discharge_current = VALUE_FROM_BMS

        # serial number of the battery (str)
        #self.serial_number = "litime_serial_1234"

        # custom field, that the user can set in the BMS software (str)
        #self.custom_field = VALUE_FROM_BMS


        # production date of the battery (str)
        #self.production = "timestr from bms"

        # hardware version of the BMS (str)
        #self.hardware_version = "harware_vers"
        #self.hardware_version = f"LiTimeBMS {self.hardware_version} {self.cell_count}S ({self.production})"

        # serial number of the battery (str)
        ##self.serial_number = VALUE_FROM_BMS Dual line
        return True

    def refresh_data(self):
        """
        call all functions that will refresh the battery data.
        This will be called for every iteration (1 second)
        Return True if success, False for failure
        """

        #result = self.read_status_data()

        # only read next dafa if the first one was successful
        #result = result and self.read_cell_data()

        self.send_com()


        # this is only an example, you can combine all into one function
        # or split it up into more functions, whatever fits best for your BMS

        return True

    def read_status_data(self):
        # read the status data
        status_data = True #self.read_serial_data_template(self.command_status)

        # check if connection was successful
        if status_data is False:
            return False

        # unpack the data
        #(
        #    value_1,
        #    value_2,
        #    value_3,
        #    value_4,
        #    value_5,
        #) = unpack_from(">bb??bhx", status_data)

        # Integrate a check to be sure, that the received data is from the BMS type you are making this driver for

        # MANDATORY values to set
        # voltage of the battery in volts (float)
        self.voltage = 26.063

        # current of the battery in amps (float)
        self.current = 0.1

        # state of charge in percent (float)
        self.soc = 100

        # temperature sensor 1 in °C (float)
        temp1 = 18
        self.to_temp(1, temp1)

        # status of the battery if charging is enabled (bool)
        self.charge_fet = True

        # status of the battery if discharging is enabled (bool)
        self.discharge_fet = True

        # OPTIONAL values to set
        # remaining capacity of the battery in ampere hours (float)
        # if not available, then it's calculated from the SOC and the capacity
        self.capacity_remaining = 98.87

        # temperature sensor 2 in °C (float)
        #temp2 = VALUE_FROM_BMS
        #self.to_temp(2, temp2)

        # temperature sensor 3 in °C (float)
        #temp3 = VALUE_FROM_BMS
        #self.to_temp(3, temp3)

        # temperature sensor 4 in °C (float)
        #temp4 = VALUE_FROM_BMS
        #self.to_temp(4, temp4)

        # temperature sensor MOSFET in °C (float)
        temp_mos = 18
        self.to_temp(0, temp_mos)

        # status of the battery if balancing is enabled (bool)
        #self.balance_fet = VALUE_FROM_BMS ##Duplicate is this required or optional?

        # PROTECTION values
        # 2 = alarm, 1 = warningm 0 = ok
        # high battery voltage alarm (int)
        #self.protection.voltage_high = VALUE_FROM_BMS What does alarm mean? and warning?

        # low battery voltage alarm (int)
        #self.protection.voltage_low = VALUE_FROM_BMS

        # low cell voltage alarm (int)
        #self.protection.voltage_cell_low = VALUE_FROM_BMS

        # low SOC alarm (int)
        #self.protection.soc_low = VALUE_FROM_BMS

        # high charge current alarm (int)
        #self.protection.current_over = VALUE_FROM_BMS

        # high discharge current alarm (int)
        #self.protection.current_under = VALUE_FROM_BMS

        # cell imbalance alarm (int)
        #self.protection.cell_imbalance = VALUE_FROM_BMS

        # internal failure alarm (int)
        #self.protection.internal_failure = VALUE_FROM_BMS

        # high charge temperature alarm (int)
        #self.protection.temp_high_charge = VALUE_FROM_BMS

        # low charge temperature alarm (int)
        #self.protection.temp_low_charge = VALUE_FROM_BMS

        # high temperature alarm (int)
        #self.protection.temp_high_discharge = VALUE_FROM_BMS

        # low temperature alarm (int)
        #self.protection.temp_low_discharge = VALUE_FROM_BMS

        # high internal temperature alarm (int)
        #self.protection.temp_high_internal = VALUE_FROM_BMS

        # fuse blown alarm (int)
        #self.protection.fuse_blown = VALUE_FROM_BMS

        # HISTORY values
        # Deepest discharge in Ampere hours (float)
        #self.history.deepest_discharge = VALUE_FROM_BMS

        # Last discharge in Ampere hours (float)
        #self.history.last_discharge = VALUE_FROM_BMS

        # Average discharge in Ampere hours (float)
        #self.history.average_discharge = VALUE_FROM_BMS

        # Number of charge cycles (int)
        #self.history.charge_cycles = VALUE_FROM_BMS

        # Number of full discharges (int)
        self.history.full_discharges = 1

        # Total Ah drawn (lifetime) (float)
        self.history.total_ah_drawn = 139

        # Minimum voltage in Volts (lifetime) (float)
        #self.history.minimum_voltage = VALUE_FROM_BMS

        # Maximum voltage in Volts (lifetime) (float)
        #self.history.maximum_voltage = VALUE_FROM_BMS

        # Minimum cell voltage in Volts (lifetime) (float)
        #self.history.minimum_cell_voltage = VALUE_FROM_BMS

        # Maximum cell voltage in Volts (lifetime) (float)
        #self.history.maximum_cell_voltage = VALUE_FROM_BMS

        # Time since last full charge in seconds (int)
        #self.history.time_since_last_full_charge = VALUE_FROM_BMS

        # Number of low voltage alarms (int)
        #self.history.low_voltage_alarms = VALUE_FROM_BMS

        # Number of high voltage alarms (int)
        #self.history.high_voltage_alarms = VALUE_FROM_BMS

        # Discharged energy in kilo Watt hours (int)
        #self.history.discharged_energy = VALUE_FROM_BMS

        # Charged energy in kilo Watt hours (int)
        #self.history.charged_energy = VALUE_FROM_BMS

        #logger.info(self.hardware_version)
        return True
