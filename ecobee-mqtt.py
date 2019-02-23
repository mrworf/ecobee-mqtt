#!/usr/bin/env python
#
#
import sys
import shelve
from datetime import datetime
import pytz
from six.moves import input
from pyecobee import *
import logging
import threading
import time
import argparse

import paho.mqtt.client as mqtt

class EcobeePoller:
  def __init__(self, application_key, thermostat_name='My Thermostat'):
    try:
      pyecobee_db = shelve.open('pyecobee_db', protocol=2)
      self.service = pyecobee_db[thermostat_name]
    except KeyError:
      if application_key is None:
        logging.error('On first run, you MUST provide API key, on subsequent runs it\'s optional')
        sys.exit(255)

      self.service = EcobeeService(thermostat_name=thermostat_name, application_key=application_key)
    finally:
      pyecobee_db.close()

    if not self.service.authorization_token:
      self.authorize()

    if not self.service.access_token:
      self.request_tokens()

  def persist_to_shelf(self, file_name):
    pyecobee_db = shelve.open(file_name, protocol=2)
    pyecobee_db[self.service.thermostat_name] = self.service
    pyecobee_db.close()


  def refresh_tokens(self):
    token_response = self.service.refresh_tokens()
    #logging.debug('TokenResponse returned from self.service.refresh_tokens():\n{0}'.format(token_response.pretty_format()))

    self.persist_to_shelf('pyecobee_db')


  def request_tokens(self):
    token_response = self.service.request_tokens()
    #logging.debug('TokenResponse returned from self.service.request_tokens():\n{0}'.format(token_response.pretty_format()))

    self.persist_to_shelf('pyecobee_db')


  def authorize(self):
    authorize_response = self.service.authorize()
    #logging.debug('AutorizeResponse returned from authorize():\n{0}'.format(authorize_response.pretty_format()))

    self.persist_to_shelf('pyecobee_db')

    logging.info('Please add EcobeeMQTT as an application. Enter PIN "%s" on the My Apps page', authorize_response.ecobee_pin)
    logging.info('Rerun EcobeeMQTT once completed.')
    sys.exit(1)

  def update_tokens(self):
    now_utc = datetime.now(pytz.utc)
    if now_utc > self.service.refresh_token_expires_on:
      logging.warning('Refresh token expired, reauthorizing')
      self.authorize()
      self.request_tokens()
    elif now_utc > self.service.access_token_expires_on:
      logging.debug('Access token expired, refreshing')
      token_response = self.refresh_tokens()

  def poll_thermostat(self):
    thermostat_summary_response = None
    try:
      thermostat_summary_response = self.service.request_thermostats_summary(selection=Selection(
      selection_type=SelectionType.REGISTERED.value,
      selection_match='',
      include_equipment_status=True))
    except EcobeeApiException as e:
      if e.status_code == 14:
        token_response = self.refresh_tokens()
        thermostat_summary_response = self.service.request_thermostats_summary(selection=Selection(
        selection_type=SelectionType.REGISTERED.value,
        selection_match='',
        include_equipment_status=True))

    if thermostat_summary_response is None:
      return {}

    # Figure out the status of the thermostat
    result = {}
    mapping = {}
    for revision in thermostat_summary_response.revision_list:
      id, name, _ = revision.split(':', 2)
      name = name.lower()
      mapping['id' + id] = name
      result[name] = []

    for status in thermostat_summary_response.status_list: 
      thermostat, status = status.split(':', 1)
      if status.strip() != '':
        status = status.split(',')
      else:
        status = []
      name = mapping['id' + thermostat]
      result[name] = status

    return result

class Reporter(threading.Thread):
  def __init__(self, ecobee, mqtt):
    threading.Thread.__init__(self)
    self.mqtt = mqtt
    self.ecobee = ecobee
    self.daemon = True
    self.start()

  def run(self):
    possible = [
      'heatPump', 
      'heatPump2', 
      'heatPump3', 
      'compCool1', 
      'compCool2', 
      'auxHeat1', 
      'auxHeat2', 
      'auxHeat3', 
      'fan', 
      'humidifier', 
      'dehumidifier', 
      'ventilator', 
      'economizer', 
      'compHotWater', 
      'auxHotWater'
    ]

    current_state = {}
    logging.info('Started reporter')
    while True:
      change = {}
      try:
        ecobee.update_tokens()
        change = ecobee.poll_thermostat()
      except:
        logging.exception('Failed to poll ecobee api')
      logging.info('Polling result: ' + repr(change))

      # First, remember old state
      old_state = current_state

      # Now, reset all known states to off
      for thermo in current_state:
        for state in current_state[thermo]:
          current_state[thermo][state] = False

      # Apply new info
      for thermo in change:
        if thermo not in current_state:
          current_state[thermo] = {}
        for state in change[thermo]:
          current_state[thermo][state] = True

      # Report changed values
      for thermo in current_state:
        for state in current_state[thermo]:
          if state not in old_state[thermo] or old_state[thermo][state] != current_state[thermo][state]:
            logging.info('Termostate %s changed state %s to %d', thermo, state, current_state[thermo][state])

      logging.info('Raw state: %s', repr(current_state))
      time.sleep(180) # 3min due to limitations of ecobee API

if __name__ == '__main__':
  logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
  parser = argparse.ArgumentParser(description="Ecobee MQTT - Track your thermostat", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument('--apikey', help='The API key for your ecobee thermostat (see README.md)')
  parser.add_argument('mqtt', help='MQTT Broker to publish topics')

  cmdline = parser.parse_args()

  ecobee = EcobeePoller(cmdline.apikey)
  client = mqtt.Client()
  #client.on_connect = on_connect
  #client.on_message = on_message
  client.connect(cmdline.mqtt, 1883, 60)

  reporter = Reporter(ecobee, mqtt)

  client.loop_forever()
