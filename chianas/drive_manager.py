#!/usr/bin/python3

# -*- coding: utf-8 -*-

__author__ = 'Richard J. Sears'
VERSION = "0.5 (2021-04-22)"

### Simple python script that helps to move my chia plots from my plotter to
### my nas. I wanted to use netcat as it was much faster on my 10GBe link than
### rsync and the servers are secure so I wrote this script to manage that
### move process. It will get better with time as I add in error checking and
### other things like notifications and stuff.


# Updates
#
#   V0.5 2021-04-22
#   - Updated to support local plot management via `move_local_plots.py`
#
#   V0.4 2021-04-13
#   - Added ability to "offline" a drive for maintenance. Before, the script would
#     select the very first available drive (based on drive number: drive0, drive1)
#     for plot storage. I ran into a problem when one of my drives kicked out a
#     smartctl error and I needed to move the plots off of it before it failed. As
#     soon as I started to move them, drive_manager.py started to fill the drive
#     back up. So now you can offline and drive_manager will not use it until
#     you online it again. You still need to go into your chia harvester config
#     and remove the drive from there.
#
#
#   V0.3 2021-04-04
#   - Added multiple command line utilities to drive_manager.py including:
#        * -dr or --drive_report    Immediately runs the Daily Report and sends email (if configured)
#        * -ct or --check_temps     Checks the temperatures of all configured plot drives
#        * -pr or --plot_report     Quick plot report like email report but to the screen
#        * -ud or --update_daily    Designed to be called from cron, updates daily plot stats (speed, etc)
#                                   Be careful if using it from the command line, it resets your stats. This
#                                   should be run once per day from a cronjob.
#
#   - Added plot time information to the daily email report including:
#        * Total Plots last 24 hours
#        * Average Plots per Hour (last 24 hours)
#        * Average Plotting Speed (last 24 hours)
#
#   V0.2 2021-30-23
#   - Moved system logging types to plot_manager_config and updated necessary functions.
#   - Added per_plot system notification function (send_new_plot_notification())
#   - Updated read_config_data() to support ConfigParser boolean returns
#   - Updated necessary functions for read_config_data() change



import os
import sys

sys.path.append('/root/plot_manager')
import subprocess
import shutil
import psutil
from pySMART import Device, DeviceList # CAUTION - DO NOT use PyPI version, use https://github.com/truenas/py-SMART
from psutil._common import bytes2human
import logging
from system_logging import setup_logging
from system_logging import read_logging_config
import system_info
from pushbullet import Pushbullet, errors as pb_errors
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
import configparser
from jinja2 import Environment, PackageLoader, select_autoescape
from datetime import datetime
from datetime import timedelta
import time
config = configparser.ConfigParser()
import argparse
import textwrap
from natsort import natsorted

# Define some colors for our help message
red='\033[0;31m'
yellow='\033[0;33m'
green='\033[0;32m'
white='\033[0;37m'
blue='\033[0;34m'
nc='\033[0m'

# Remove if not using Sentry.io
# also remove capture(e) in functions below.
import sentry_sdk

sentry_sdk.init(
    "https://kjhsflkjshdflkjhdslkfjhaslkdjfhlakjsd.ingest.sentry.io/98793879",

    # Set traces_sample_rate to 1.0 to capture 100%
    # of transactions for performance monitoring.
    # We recommend adjusting this value in production.
    traces_sample_rate=1.0
)
from sentry_sdk import capture_exception

# Let's do some housekeeping
nas_server = 'chianas01'
plot_size_k = 108995911228
plot_size_g = 101.3623551
receive_script = '/root/plot_manager/receive_plot.sh'

today = datetime.today().strftime('%A').lower()
current_military_time = datetime.now().strftime('%H:%M:%S')
current_timestamp = int(time.time())

# Setup Module logging. Main logging is configured in system_logging.py
setup_logging()
level = read_logging_config('plot_manager_config', 'system_logging', 'log_level')
level = logging._checkLevel(level)
log = logging.getLogger(__name__)
log.setLevel(level)


# Define our help message
class RawFormatter(argparse.HelpFormatter):
    def _fill_text(self, text, width, indent):
        return "\n".join(
            [textwrap.fill(line, width) for line in textwrap.indent(textwrap.dedent(text), indent).splitlines()])

program_descripton = f'''
                {red}******** {green}ChiaNAS Drive Manager{nc} - {blue}{VERSION}{red} ********{nc}
    Running drive_manager.py with no arguments causes drive_manager to run in '{yellow}normal{nc}' mode.
    In this mode {green}drive_manager{nc} will check the drive utilization and update which drive your
    Chia plots will be sent to when they arrive from your plotter. This is generally called
    from a cronjob on a regular basis. Please read the full information about how it works
    on my github page.

    
    There are several commandline switches you can use to get immediate reports and feedback:
    

    {green}-dr {nc}or{green} --drive_report{nc}       {blue}Runs the Daily ChiaNAS Report (if configured), and emails
                                it to you. This can be called from a crontab job as well.{nc}
    
    {green}-ct {nc}or{green} --check_temps{blue}        This will query all of your hard drives using {yellow}smartctl{blue} and
                                return a list of drive temperatures to you.
                                
    {green}-pr {nc}or{green} --plot_report{blue}        This queries the NAS and returns a report letting you know 
                                how many plots are currently on the system and how many more
                                you can add based on the current drive configuration. It also
                                includes plotting speed information for the last 24 hours.{nc}
    
    {green}-ud {nc}or{green} --update_daily{blue}       This updates the total number of plots the system has created
                                over the past 24 hours. Use with {nc}CAUTION!{blue}. This {nc}should{blue} be ran
                                from crontab once every 24 hours only! It updates the total
                                from the last time is was run until now, hence why you should
                                only run this once per 24 hours.{nc}
    
    {green}-off {nc}or{green} --offline_hdd{blue}       This takes a drive as it's input (for example {yellow} drive6{blue}) and
                                "{red}offlines{blue}" it so that no more plots will get written to it. 
                                You must {green}--on{blue} or {green}--online_hdd{blue} the drive for it to be used
                                again. Useful if the drive is failing and needs to be replaced.
                                You cannot "{red}offline{blue} a drive that is not mounted.
    
    {green}-on {nc}or{green} --online_hdd{blue}         This takes a drive as it's input (for example {yellow} drive6{blue}) and
                                "{green}onlines{blue}" it so that plots will get written to it. This option
                                will be {nc}UNAVAILABLE{blue} if there are no drives that have been 
                                offlined!{nc}
                                

    USAGE:
    '''

# Grab command line arguments if there are any
def init_argparser():
    with open('offlined_drives', 'r') as offlined_drives_list:
        offlined_drives = [current_drives.rstrip() for current_drives in offlined_drives_list.readlines()]
    parser = argparse.ArgumentParser(description=program_descripton, formatter_class=RawFormatter)
    parser.add_argument('-v', '--version', action='version', version=f'{parser.prog} {VERSION}')
    parser.add_argument('-dr', '--daily_report', action='store_true', help='Run the ChiaPlot Daily Email Report and exit')
    parser.add_argument('-ct', '--check_temps', action='store_true', help='Return a list of drives and their temperatures and exit')
    parser.add_argument('-pr', '--plot_report', action='store_true', help='Return the total # of plots on the system and total you can add and exit')
    parser.add_argument('-ud', '--update_daily', action='store_true', help=f'Updates 24 hour plot count. {red}USE WITH CAUTION, USE WITH CRONTAB{nc}')
    parser.add_argument('-off', '--offline_hdd', action='store', help=f'Offline a specific drive. Use drive number: {green}drive6{nc}')
    if offlined_drives != []:
        parser.add_argument('-on', '--online_hdd', action='store', help=f'Online a specific drive.' , choices=offlined_drives)
    return parser


def get_offlined_drives():
    with open('offlined_drives', 'r') as offlined_drives_list:
        offlined_drives = [current_drives.rstrip() for current_drives in offlined_drives_list.readlines()]
        if offlined_drives != None:
            return offlined_drives
        else:
            return False


# Setup to read and write to our config file.
# If we are expecting a boolean back pass True/1 for bool,
# otherwise False/0
def read_config_data(file, section, item, bool):
    pathname = '/root/plot_manager/' + file
    config.read(pathname)
    if bool:
        return config.getboolean(section, item)
    else:
        return config.get(section, item)


def update_config_data(file, section, item, value):
    pathname = '/root/plot_manager/' + file
    config.read(pathname)
    cfgfile = open(pathname, 'w')
    config.set(section, item, value)
    config.write(cfgfile)
    cfgfile.close()


def get_drive_info(action, drive):
    """
    This allows us to query specific information about our drives including
    temperatures, smart assessments, and space available to use for plots.
    It allows us to simply hand it a drive number (drive0, drive22, etc)
    and will present us with the data back. This utilizes pySMART, but
    a word of caution, use the TrueNAS versions linked to above, the PiPy
    version has a bug!
    """
    if action == 'device':
        plot_drives = get_list_of_plot_drives()
        device = [hd for hd in plot_drives
                  if hd[0] == (get_mountpoint_by_drive_number(drive)[0])]
        if device != []:
            device = [hd for hd in plot_drives
                      if hd[0] == (get_mountpoint_by_drive_number(drive)[0])]
        return device[0][1]
    if action == 'temperature':
        return Device(get_device_info_by_drive_number(drive)[0][1]).temperature
    if action == 'capacity':
        return Device(get_device_info_by_drive_number(drive)[0][1]).capacity
    if action == 'health':
        return Device(get_device_info_by_drive_number(drive)[0][1]).assessment
    if action == 'name':
        return Device(get_device_info_by_drive_number(drive)[0][1]).name
    if action == 'serial':
        return Device(get_device_info_by_drive_number(drive)[0][1]).serial
    if action == 'space_total':
        return int(bytesto(shutil.disk_usage(get_device_info_by_drive_number(drive)[0][0])[0], 'g'))
    if action == 'space_used':
        return int(bytesto(shutil.disk_usage(get_device_info_by_drive_number(drive)[0][0])[1], 'g'))
    if action == 'space_free':
        return int(bytesto(shutil.disk_usage(get_device_info_by_drive_number(drive)[0][0])[2], 'g'))
    if action == 'space_free_plots':
        return int(bytesto(shutil.disk_usage(get_device_info_by_drive_number(drive)[0][0])[2], 'g') / plot_size_g)
    if action == 'space_free_plots_by_mountpoint':
        return int(bytesto(shutil.disk_usage(drive)[2], 'g') / plot_size_g)
    if action == 'total_current_plots':
        return int(bytesto(shutil.disk_usage(get_mountpoint_by_drive_number(drive)[0])[1], 'g') / plot_size_g)
    if action == 'total_current_plots_by_mountpoint':
        return int(bytesto(shutil.disk_usage(drive)[1], 'g') / plot_size_g)



def dev_test(drive):
    return shutil.disk_usage(drive)
    #return Device(drive)

def get_drive_by_mountpoint(mountpoint):
    """
    This accepts a mountpoint ('/mnt/enclosure0/rear/column2/drive32') and returns the drive:
    drive32
    """
    return (mountpoint.split("/")[5])

def get_mountpoint_by_drive_number(drive):
    """
    This accepts a drive number (drive0) and returns the device assignment: /dev/sda1 and mountpoint:
    /mnt/enclosure0/front/column0/drive0
    """
    partitions = psutil.disk_partitions(all=False)
    for p in partitions:
        if p.device.startswith('/dev/sd') and p.mountpoint.startswith('/mnt/enclosure') and p.mountpoint.endswith(drive):
            return [(p.mountpoint)]


def get_device_info_by_drive_number(drive):
    """
    This accepts a drive number (drive0) and returns the device assignment: /dev/sda1 and mountpoint
    """
    partitions = psutil.disk_partitions(all=False)
    for p in partitions:
        if p.device.startswith('/dev/sd') and p.mountpoint.startswith('/mnt/enclosure') and p.mountpoint.endswith(drive):
            return [(p.mountpoint, p.device)]


def get_device_by_mountpoint(mountpoint):
    """
        This accepts a mountpoint and returns the device assignment: /dev/sda1 and mountpoint
        """
    partitions = psutil.disk_partitions(all=False)
    for p in partitions:
        if p.device.startswith('/dev/sd') and p.mountpoint.startswith(mountpoint):
            return [(p.mountpoint, p.device)]

def get_mountpoint_by_device(device):
    """
        This accepts a mountpoint and returns the device assignment: /dev/sda1 and mountpoint
        """
    partitions = psutil.disk_partitions(all=False)
    for p in partitions:
        if p.device.startswith(device):
            return [(p.mountpoint, p.device)]

def get_list_of_plot_drives():
    """
    Return list of tuples of all available plot drives on the system and the device assignment
    [('/mnt/enclosure0/front/column0/drive3', '/dev/sde1')]
    ===> Currently Unused
    """
    partitions = psutil.disk_partitions(all=False)
    mountpoint = []
    for p in partitions:
        if p.device.startswith('/dev/sd') and p.mountpoint.startswith('/mnt/enclosure'):
            mountpoint.append((p.mountpoint, p.device, p.fstype))
    return mountpoint


# Thank you: https://gist.github.com/shawnbutts/3906915
def bytesto(bytes, to, bsize=1024):
    a = {'k': 1, 'm': 2, 'g': 3, 't': 4, 'p': 5, 'e': 6}
    r = float(bytes)
    return bytes / (bsize ** a[to])


def get_all_available_system_space(type):
    """
    Return Systems drive space information (total, used and free) based on plot_size
    """
    partitions = psutil.disk_partitions(all=False)
    drive_space_available = []
    for p in partitions:
        if p.device.startswith('/dev/sd') and p.mountpoint.startswith('/mnt/enclosure'):
            if type == 'all':
                drive_space_available.append((p.mountpoint, shutil.disk_usage(p.mountpoint)))
            if type == 'total':
                drive_space_available.append(int(bytesto(shutil.disk_usage(p.mountpoint)[0], 'g') / plot_size_g))
            if type == 'used':
                drive_space_available.append(int(bytesto(shutil.disk_usage(p.mountpoint)[1], 'g') / plot_size_g))
            if type == 'free':
                drive_space_available.append(int(bytesto(shutil.disk_usage(p.mountpoint)[2], 'g') / plot_size_g))
    return len(drive_space_available), sum(drive_space_available)


def get_plot_drive_with_available_space():
    """
    This looks at all available plot drives that start with /dev/sd and include
    /mnt/enclosure in the mount path (this covers all of my plot drives), it then
    looks for any drive that has enough space for at least one plot (k32), sorts
    that list based on the /dev/sdx sorting and then returns the mountpoint and
    the device of each drive.
    ======> Currently Unused <======
    """
    available_drives = []
    for part in psutil.disk_partitions(all=False):
        if part.device.startswith('/dev/sd') and part.mountpoint.startswith('/mnt/enclosure') and get_drive_info(
                'space_free_plots_by_mountpoint', part.mountpoint) >= 1:
            available_drives.append((part.mountpoint, part.device))
    return (sorted(available_drives, key=lambda x: x[1]))


def get_plot_drive_to_use():
    """
        This looks at all available plot drives that start with /dev/sd and include
        /mnt/enclosure in the mount path (this covers all of my plot drives), it then
        looks for any drive that has enough space for at least one plot (k32), sorts
        that list based on the drive# sorting (drive0, drive10, etc) sorting and then
        returns the mountpoint of the device we want to use. Basically the same as above
        but simply returns the 'next' available drive we want to use. This also checks
         to make sure the drive selected has not been marked as "offline".
        #TODO incorporate in get_plot_drive_with_available_space()
        """
    with open('offlined_drives', 'r') as offlined_drives_list:
        offlined_drives = [current_drives.rstrip() for current_drives in offlined_drives_list.readlines()]
    available_drives = []
    for part in psutil.disk_partitions(all=False):
        if part.device.startswith('/dev/sd') \
                and part.mountpoint.startswith('/mnt/enclosure') \
                and get_drive_info('space_free_plots_by_mountpoint', part.mountpoint) >= 1 \
                and get_drive_by_mountpoint(part.mountpoint) not in offlined_drives:
            drive = get_drive_by_mountpoint(part.mountpoint)
            available_drives.append((part.mountpoint, part.device, drive))
    return (natsorted(available_drives)[0][0])

def get_sorted_drive_list():
    """
    Returns sorted list of drives
    """
    available_drives = []
    for part in psutil.disk_partitions(all=False):
        if part.device.startswith('/dev/sd') and part.mountpoint.startswith('/mnt/enclosure'):
            drive=get_drive_by_mountpoint(part.mountpoint)
            available_drives.append((part.mountpoint, part.device, drive))
    return natsorted(available_drives)

def get_current_plot_drive_info():
    """
    Designed for debugging and logging purposes when we switch drives
    """
    return Device(get_device_by_mountpoint(get_plot_drive_to_use())[0][1]).temperature


def log_drive_report():
    """
    Logs a drive report of our newly selected plot drive
    """
    templ = "%-15s %6s %15s %12s %10s  %5s"
    log.info(templ % ("New Plot Drive", "Size", "Avail Plots", "Serial #", "Temp °C",
                      "Mount Point"))

    usage = psutil.disk_usage(get_device_by_mountpoint(get_plot_drive_to_use())[0][0])

    log.info(templ % (
        get_device_by_mountpoint(get_plot_drive_to_use())[0][1],
        bytes2human(usage.total),
        get_drive_info('space_free_plots_by_mountpoint', (get_plot_drive_to_use())),
        Device(get_device_by_mountpoint(get_plot_drive_to_use())[0][1]).serial,
        Device(get_device_by_mountpoint(get_plot_drive_to_use())[0][1]).temperature,
        get_device_by_mountpoint(get_plot_drive_to_use())[0][0]))


def online_offline_drive(drive, onoffline):
    log.debug(f'online_offline_drive() called with [{drive}] , [{onoffline}]')
    if get_device_info_by_drive_number(drive) == None:
        print()
        print(f'{red}WARNING{nc}: {blue}{drive}{nc} does not exist or is not mounted on this system!')
        print()
        log.debug(f'Drive: {drive} does not exist or is not mounted on this system!')
    else:
        if onoffline == 'offline':
            offlined_drives = []
            with open('offlined_drives', 'r') as offlined_drives_list:
                offlined_drives = [current_drives.rstrip() for current_drives in offlined_drives_list.readlines()]
                if drive in offlined_drives:
                    print()
                    print(f'Drive: {blue}{drive}{nc} Already in {red}OFFLINE{nc} mode! No action taken.')
                    print()
                    log.debug(f'Drive: {drive} Already in offline mode!')
                else:
                    offlined_drives.append(drive)
                    with open('offlined_drives', 'w') as offlined_drive_list:
                        offlined_drive_list.writelines("%s\n"  % drives for drives in offlined_drives)
                        print()
                        print(f'Drive: {blue}{drive}{nc} Put into {red}OFFLINE{nc} mode! Plots will not be written to this drive!')
                        print()
                        log.debug(f'Drive: {drive} Put into OFFLINE mode! Plots will not be written to this drive!')
        elif onoffline == 'online':
            offlined_drives = []
            with open('offlined_drives', 'r') as offlined_drives_list:
                offlined_drives = [current_drives.rstrip() for current_drives in offlined_drives_list.readlines()]
                if drive in offlined_drives:
                    offlined_drives.remove(drive)
                    with open('offlined_drives', 'w') as offlined_drive_list:
                        offlined_drive_list.writelines("%s\n"  % drives for drives in offlined_drives)
                        print()
                        print(f'Drive: {blue}{drive}{nc} Put into {green}ONLINE{nc} mode! Plots will now be written to this drive!')
                        print()
                        log.debug(f'Drive: {drive} Put into ONLINE mode! Plots will now be written to this drive!')
                else:
                    print()
                    print(f'Drive: {blue}{drive}{nc} was not in {red}OFFLINE{nc} mode! No action taken.')
                    print()
                    log.debug(f'Drive: {drive} was not offline!')
        elif onoffline == 'check':
            with open('offlined_drives', 'r') as offlined_drives_list:
                offlined_drives = [current_drives.rstrip() for current_drives in offlined_drives_list.readlines()]
                if drive in offlined_drives:
                    return True
                else:
                    return False

def update_receive_plot():
    """
    This utilizes the get_plot_drive_to_use() function and builds out
    our netcat receive_plot.sh script that is called by our plotting
    server when it is ready to send over a new plot. The plotting server
    sends the plot 'in the blind' so-to-speak, this function determines
    what drive the plot will go on and updates the receive shell script
    accordingly. Eventually I will do all of the netcat within the script
    here. See TODO: Update to use netcat native to python.
    """

    log.debug("update_receive_plot() Started")
    total_serverwide_plots = get_all_available_system_space('used')[1]
    log.debug(f'Total Serverwide Plots: {total_serverwide_plots}')
    # First determine if there is a remote file transfer in process. If there is, pass until it is done:
    if os.path.isfile(read_config_data('plot_manager_config', 'remote_transfer', 'remote_transfer_active', False)):
        log.debug('Remote Transfer in Progress, will try again soon!')
        quit()
    else:
        current_plotting_drive = read_config_data('plot_manager_config', 'plotting_drives', 'current_plotting_drive', False)
        if current_plotting_drive == get_plot_drive_to_use():
            log.debug(f'Currently Configured Plot Drive: {current_plotting_drive}')
            log.debug(f'System Selected Plot Drive:      {get_plot_drive_to_use()}')
            log.debug('Configured and Selected Drives Match!')
            log.debug(f'No changes necessary to {receive_script}')
            log.debug(
                f'Plots left available on configured plotting drive: {get_drive_info("space_free_plots_by_mountpoint", current_plotting_drive)}')
        else:
            send_new_plot_disk_email()  # This is the full Plot drive report. This is in addition to the generic email sent by the
            # notify() function.
            notify('Plot Drive Updated', f'Plot Drive Updated: Was: {current_plotting_drive},  Now: {get_plot_drive_to_use()}')
            f = open(receive_script, 'w+')
            f.write('#! /bin/bash \n')
            f.write(f'nc -l -q5 -p 4040 > "{get_plot_drive_to_use()}/$1" < /dev/null')
            f.close()
            update_config_data('plot_manager_config', 'plotting_drives', 'current_plotting_drive', get_plot_drive_to_use())
            log.info(f'Updated {receive_script} and system config file with new plot drive.')
            log.info(f'Was: {current_plotting_drive},  Now: {get_plot_drive_to_use()}')
            log_drive_report()


def send_new_plot_disk_email():
    usage = psutil.disk_usage(get_device_by_mountpoint(get_plot_drive_to_use())[0][0])
    current_plotting_drive = read_config_data('plot_manager_config', 'plotting_drives', 'current_plotting_drive', False)
    if read_config_data('plot_manager_config', 'notifications', 'new_plot_drive', True):
        for email_address in system_info.alert_email:
            send_template_email(template='new_plotting_drive.html',
                                recipient=email_address,
                                subject='New Plotting Drive Selected\nContent-Type: text/html',
                                current_time=current_military_time,
                                nas_server=nas_server,
                                previous_plotting_drive=current_plotting_drive,
                                plots_on_previous_plotting_drive=get_drive_info('total_current_plots_by_mountpoint',current_plotting_drive),
                                current_plotting_drive_by_mountpoint=get_plot_drive_to_use(),
                                current_plotting_drive_by_device=get_device_by_mountpoint(get_plot_drive_to_use())[0][1],
                                drive_size=bytes2human(usage.total),
                                plots_available=get_drive_info('space_free_plots_by_mountpoint', (get_plot_drive_to_use())),
                                drive_serial_number=Device(get_device_by_mountpoint(get_plot_drive_to_use())[0][1]).serial,
                                current_drive_temperature=Device(get_device_by_mountpoint(get_plot_drive_to_use())[0][1]).temperature,
                                smart_health_assessment=Device(get_device_by_mountpoint(get_plot_drive_to_use())[0][1]).assessment,
                                total_serverwide_plots=get_all_available_system_space('used')[1],
                                total_number_of_drives=get_all_available_system_space('total')[0],
                                total_k32_plots_until_full=get_all_available_system_space('free')[1],
                                max_number_of_plots=get_all_available_system_space('total')[1],
                                days_to_fill_drives=(int(get_all_available_system_space('free')[1] / int(read_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_daily', False)))))
    else:
        pass


def send_daily_update_email():
    usage = psutil.disk_usage(get_device_by_mountpoint(get_plot_drive_to_use())[0][0])
    if read_config_data('plot_manager_config', 'notifications', 'daily_update', True):
        for email_address in system_info.alert_email:
            send_template_email(template='daily_update.html',
                                recipient=email_address,
                                subject='NAS Server Daily Update\nContent-Type: text/html',
                                current_time=current_military_time,
                                nas_server=nas_server, current_plotting_drive_by_mountpoint=get_plot_drive_to_use(),
                                current_plotting_drive_by_device=get_device_by_mountpoint(get_plot_drive_to_use())[0][1],
                                drive_size=bytes2human(usage.total),
                                drive_serial_number=Device(get_device_by_mountpoint(get_plot_drive_to_use())[0][1]).serial,
                                current_drive_temperature=Device(get_device_by_mountpoint(get_plot_drive_to_use())[0][1]).temperature,
                                smart_health_assessment=Device(get_device_by_mountpoint(get_plot_drive_to_use())[0][1]).assessment,
                                total_serverwide_plots=get_all_available_system_space('used')[1],
                                total_number_of_drives=get_all_available_system_space('total')[0],
                                total_k32_plots_until_full=get_all_available_system_space('free')[1],
                                max_number_of_plots=get_all_available_system_space('total')[1],
                                total_plots_last_day=read_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_daily', False),
                                days_to_fill_drives=(int(get_all_available_system_space('free')[1] / int(read_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_daily', False)))),
                                average_plots_per_hour=round((int(read_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_daily', False)))/24,1),
                                average_plotting_speed=(int(read_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_daily', False)) * int(plot_size_g)/1000))
    else:
        pass

def space_report():
    print('')
    print(f'{blue}############################################################{nc}')
    print(f'{blue}################### {green}{nas_server} Plot Report{blue} ##################{nc}' )
    print(f'{blue}############################################################{nc}')
    print (f'Total Number of Plots on {green}{nas_server}{nc}:                     {yellow}{get_all_available_system_space("used")[1]}{nc}')
    print (f'Total Number of Systemwide Plots Drives:                  {yellow}{get_all_available_system_space("total")[0]}{nc}')
    print (f'Total Number of k32 Plots until full:                   {yellow}{get_all_available_system_space("free")[1]}{nc}')
    print (f'Maximum # of plots when full:                           {yellow}{get_all_available_system_space("total")[1]}{nc}')
    print (f"Plots completed in the last 24 Hours:                     {yellow}{int(read_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_daily', False))}{nc}")
    print (f"Average Plots per Hours:                                 {yellow}{round((int(read_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_daily', False)))/24,1)}{nc}")
    print (f"Average Plotting Speed Last 24 Hours (TiB/Day):         {yellow}{round((int(read_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_daily', False)) * int(plot_size_g)/1000),2)}{nc} ")
    print(f"Appx Number of Days to fill all current plot drives:     {yellow} {int(get_all_available_system_space('free')[1] / int(read_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_daily', False)))} {nc} ")
    print (f"Current Plot Storage Drive:                       {yellow}{(get_device_by_mountpoint(read_config_data('plot_manager_config', 'plotting_drives', 'current_plotting_drive', False))[0][1])}{nc}")
    print (f"Temperature of Current Plot Drive:                      {yellow}{Device((get_device_by_mountpoint(read_config_data('plot_manager_config', 'plotting_drives', 'current_plotting_drive', False))[0][1])).temperature}°C{nc}")
    print (f"Latest Smart Drive Assessment of Plot Drive:            {yellow}{Device((get_device_by_mountpoint(read_config_data('plot_manager_config', 'plotting_drives', 'current_plotting_drive', False))[0][1])).assessment}{nc}")
    print(f'{blue}############################################################{nc}')
    print('')
    print('')

def temperature_report():
    print('')
    print(f'{blue}#################################################################{nc}')
    print(f'{blue}################# {green}{nas_server} Temperature Report {blue}##################{nc}')
    print(f'{blue}#################################################################{nc}')
    print(f'{blue}#    {nc}Serial#{blue}     #{nc}     Device{blue}     #{nc}     Drive{blue}     #{nc}    Temp{blue}     #{nc}')
    print(f'{blue}#################################################################{nc}')
    for drive in get_sorted_drive_list():
        print(f'{blue}#{nc}   {Device(drive[1]).serial}'f'{blue}     #{nc}'f'   {drive[1]}{blue}    #{nc}' f'    {((get_drive_by_mountpoint(drive[0])))}{blue}    #{nc}' f'     {Device(drive[1]).temperature}°C'f'{blue}     #{nc}')
    print(f'{blue}##################################################################{nc}')
    print('')
    print('')

# You should run this once per day to sse total daily plots
# in your reports. If you run it more often, the numbers will
# not be correct. I use midnight here for my purposes, but
# this is just a var name.
def update_daily_plot_counts():
    current_total_plots_midnight = int(read_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_midnight', False))
    total_serverwide_plots = get_all_available_system_space('used')[1]
    update_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_midnight', str(total_serverwide_plots))
    total_plots_daily = (total_serverwide_plots - current_total_plots_midnight)
    update_config_data('plot_manager_config', 'plotting_information', 'current_total_plots_daily', str(total_plots_daily))


def send_email(recipient, subject, body):
    """
    Part of our notification system.
    Setup to send email via the builtin linux mail command.
    Your local system **must** be configured already to send mail or this will fail.
    https://stackoverflow.com/questions/27874102/executing-shell-mail-command-using-python
    https://nedbatchelder.com/text/unipain.html
    https://www.digitalocean.com/community/tutorials/how-to-install-and-configure-postfix-as-a-send-only-smtp-server-on-ubuntu-20-04
    """
    try:
        subprocess.run(['mail', '-s', subject, recipient], input=body, encoding='utf-8')
        log.debug(f"Email Notification Sent: Subject: {subject}, Recipient: {recipient}, Message: {body}")
    except subprocess.CalledProcessError as e:
        log.debug(f'send_email error: {e}')
        capture_exception(e)
    except Exception as e:
        log.debug(f'send_email: Unknown Error! Email not sent.')
        capture_exception(e)


# Setup to send out Pushbullet alerts. Pushbullet config is in system_info.py
def send_push_notification(title, message):
    """Part of our notification system. This handles sending PushBullets."""
    try:
        pb = Pushbullet(system_info.pushbilletAPI)
        push = pb.push_note(title, message)
        log.debug(f"Pushbullet Notification Sent: {title} - {message}")
    except pb_errors.InvalidKeyError as e:
        log.debug(f'Pushbullet Exception: Invalid API Key! Message not sent.')
        capture_exception(e)
    except Exception as e:
        log.debug(f'Pushbullet Exception: Unknown Pushbullet Error: {e}. Message not sent.')
        capture_exception(e)


def send_sms_notification(body, phone_number):
    """Part of our notification system. This handles sending SMS messages."""
    try:
        client = Client(system_info.twilio_account, system_info.twilio_token)
        message = client.messages.create(to=phone_number, from_=system_info.twilio_from, body=body)
        log.debug(f"SMS Notification Sent: {body}.")
    except TwilioRestException as e:
        log.debug(f'Twilio Exception: {e}. Message not sent.')
        capture_exception(e)
    except Exception as e:
        log.debug(f'Twilio Exception: {e}. Message not sent.')
        capture_exception(e)


def notify(title, message):
    """ Notify system for email, pushbullet and sms (via Twilio)"""
    log.debug(f'notify() called with Title: {title} and Message: {message}')
    if (read_config_data('plot_manager_config', 'notifications', 'alerting', True)):
        if (read_config_data('plot_manager_config', 'notifications', 'pb', True)):
            send_push_notification(title, message)
        if (read_config_data('plot_manager_config', 'notifications', 'email', True)):
            for email_address in system_info.alert_email:
                send_email(email_address, title, message)
        if (read_config_data('plot_manager_config', 'notifications', 'sms', True)):
            for phone_number in system_info.twilio_to:
                send_sms_notification(message, phone_number)
    else:
        pass


# Thank You - https://frankcorso.dev/email-html-templates-jinja-python.html
def send_template_email(template, recipient, subject, **kwargs):
    """Sends an email using a jinja template."""
    env = Environment(
        loader=PackageLoader('drive_manager', 'templates'),
        autoescape=select_autoescape(['html', 'xml'])
    )
    template = env.get_template(template)
    send_email(recipient, subject, template.render(**kwargs))

# This function called from crontab. First run the daily update (-ud) then (-dr):
# 01 00 * * * /usr/bin/python3 /root/plot_manager/drive_manager.py -ud >/dev/null 2>&1
# 02 00 * * * /usr/bin/python3 /root/plot_manager/drive_manager.py -dr >/dev/null 2>&1
def send_daily_email():
    log.debug('send_daily_email() Started')
    send_daily_update_email()
    log.info('Daily Update Email Sent!')

def send_new_plot_notification():
    log.debug('send_new_plot_notification() Started')
    if os.path.isfile('new_plot_received'):
        log.debug('New Plot Received')
        if read_config_data('plot_manager_config', 'notifications', 'per_plot', True):
            notify('New Plot Received', 'New Plot Received')
        os.remove('new_plot_received')

def main():
    parser = init_argparser()
    args = parser.parse_args()
    if args.daily_report:
        send_daily_email()
    elif args.plot_report:
        space_report()
    elif args.update_daily:
        update_daily_plot_counts()
    elif args.check_temps:
        temperature_report()
    elif args.offline_hdd:
        online_offline_drive(args.offline_hdd, 'offline')
    elif get_offlined_drives():
        if args.online_hdd:
            online_offline_drive(args.online_hdd, 'online')
        else:
            send_new_plot_notification()
            update_receive_plot()
    else:
       send_new_plot_notification()
       update_receive_plot()


if __name__ == '__main__':
    main()
