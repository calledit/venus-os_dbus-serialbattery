# -*- coding: utf-8 -*-
from battery import Battery, Cell
from utils import logger
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
            logger.error("BLE connection to BMS took to long to inititate")
            return False

        self.send_com()
        self.get_settings()

        return True

    def client_disconnected(self, client):
        logger.error("BMS disconnected")

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
        self.client = BleakClient(address, disconnected_callback=self.client_disconnected)
        try:
            logger.info("initiate BLE connection to: "+address)
            await self.client.connect()
            logger.info("connected")
            await self.client.start_notify(self.read_characteristic, self.notify_read_callback)

        except Exception as e:
            logger.error("Failed when trying to connect", e)
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
        self.parse_status(data)

    def unique_identifier(self) -> str:
        return self.address

    def connection_name(self) -> str:
      return "BLE " + self.address

    def custom_name(self) -> str:
        return "Bat: " + self.type + " " + self.address[-5:]

    def parse_status(self, data):
        messured_total_voltage, cells_added_together_voltage = unpack_from("II", data, 8)
        messured_total_voltage /= 1000
        cells_added_together_voltage /= 1000

        heat, balance_memory_active, protection_state, failure_state, is_balancing, battery_state, SOC, SOH, discharges_count, discharges_amph_count = unpack_from("IIIIIHHIII", data, 68)

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

        current, cell_temp, mosfet_temp, unknown_temp, not_known1, not_known2, remaining_amph, full_charge_capacity_amph, not_known3 = unpack_from("ihhhHHHHH", data, 48)

        #current sensor is very inaccurate
        current = current/1000

        remaining_amph /= 100
        full_charge_capacity_amph /= 100

        #Debug data
        #print(f"current: {current}, cell_temp: {cell_temp}, mosfet_temp: {mosfet_temp}, unknown_temp: {unknown_temp}, not_known1: {not_known1}, not_known2: {not_known2}")
        #print(f"remaining_amph: {remaining_amph}, full_charge_capacity_amph: {full_charge_capacity_amph}, not_known3: {not_known3}")
        #print(f"heat: {heat}, b_m_a: {balance_memory_active}, protection_state: {protection_state}, failure_state: {failure_state}, is_balancing: {is_balancing}, battery_state: {battery_state}, SOC: {SOC}, SOH: {SOH}, discharges_count: {discharges_count}, discharges_amph_count: {discharges_amph_count}")

        self.capacity = full_charge_capacity_amph
        self.voltage = messured_total_voltage
        self.soc = SOC

        if is_balancing != 0:
             self.balance_fet = True
        else:
             self.balance_fet = False

        #Debug data
        #f = open("/data/charge_log.txt", "a")
        #timestr = time.ctime()
        #f.write(f"timestr: {timestr} curr: {current}, nk1: {not_known1}, nk2: {not_known2}, n3: {not_known3},  SOC: {SOC}, tot_v: {messured_total_voltage}, add_v: {cells_added_together_voltage}, protect_state: {protection_state}, fail_state: {failure_state}, is_bal: {bin(is_balancing)}, bat_st: {battery_state}, heat: {heat}, b_m_a: {balance_memory_active}, rem_ah: {remaining_amph} {cellv_str}\n")
        #f.close()

        #Due to the fact that the current reading is very inacurare we try to calculate current draw from remaining_amph
        current_based_on_remaning = 0
        if self.Last_remianAh == 0:
            self.current = 0
            self.Last_remianAh = remaining_amph
            self.Last_remianAh_time = time.time()

        now_time = time.time()
        time_since_last_update = int(now_time - self.Last_remianAh_time)
        if self.Last_remianAh != remaining_amph:
            Last_remianAh_time_diff = float(now_time - self.Last_remianAh_time)/3600
            Last_remianAh_change_diff = remaining_amph - self.Last_remianAh
            self.Last_remianAh = remaining_amph
            self.Last_remianAh_time = now_time
            if self.Last_remianAh_initiation == 0:#since we dont know how long the last reasing has been active we need to wait for another reading
                self.Last_remianAh_initiation = 1
            else:
                self.current_based_on_remaning = Last_remianAh_change_diff/Last_remianAh_time_diff
                self.Last_remianAh_initiation = 2

        #Calculate average current over last 5 messurments due to sensor inacuracy
        self.last_few_currents.append(current)
        if len(self.last_few_currents) > 5:
            self.last_few_currents.pop(0)

        Last_few_avg = sum(self.last_few_currents)/len(self.last_few_currents)

        Use_Reason = ""
        #if last update was long ago we use the current reported by the bms despite it beeing unstable, we also use the current from the BMS if there is a very large discrepency betwen them
        if time_since_last_update > 25:
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

        #Debug data
        #logger.info(f"{Use_Reason}, current:{current:.3f}, Last_few_avg: {Last_few_avg:.3f}, base: {self.current_based_on_remaning:.3f}")


        # status of the battery if charging is enabled (bool)
        self.charge_fet = True
        if battery_state == 4:
            self.charge_fet = False

        # status of the battery if discharging is enabled (bool) (there might be other values of heat or battery_state that could indicate that discharge is disabled)
        self.discharge_fet = True
        if heat == 0x80 or protection_state in (0x20, 0x80):
            self.discharge_fet = False

        # temperature sensor 1 in °C (float)
        temp1 = cell_temp
        self.to_temp(1, temp1)

        # temperature sensor 2 in °C (float)
        temp2 = unknown_temp
        self.to_temp(2, temp2)

        # temperature sensor MOSFET in °C (float)
        temp_mos = mosfet_temp
        self.to_temp(0, temp_mos)

        self.capacity_remaining = remaining_amph
        self.history.total_ah_drawn = discharges_amph_count
        self.history.full_discharges = discharges_count

    def get_settings(self):

        self.max_battery_voltage = utils.MAX_CELL_VOLTAGE * self.cell_count
        self.min_battery_voltage = utils.MIN_CELL_VOLTAGE * self.cell_count
        return True

    def refresh_data(self):
        """
        This is called each time the library wants data (1 second)
        """

        self.send_com()

        return True
