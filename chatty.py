#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import appdaemon.plugins.hass.hassapi as hass
import slixmpp

class Chatty(hass.Hass):
    class Command:
        def __init__(self, name, callback):
            self.name = name
            self.callback = callback

    def register_command(self, name, callback):
        self.commands.append(Chatty.Command(name, callback))

    async def initialize(self):
        username = self.args["username"]
        password = self.args["password"]
        self.recipients = self.args["recipients"] # note, this is expected to be an array!

        self.commands = list()

        self.start_xmpp(username, password)

        self.register_service("notify/jabber", self.on_notify_service)
        self.listen_event(self.on_notify_event, "NOTIFY_JABBER")

        self.log("Chatty started.")

        # register commands
        self.mycommands = MyCommands(self)

    def start_xmpp(self, username, password):
        self.log("Starting chatty with username: {}".format(username))
        
        self.xmpp = XMPPconnector(username, password, self)
        self.xmpp.register_plugin('xep_0030') # Service Discovery
        self.xmpp.register_plugin('xep_0004') # Data Forms
        self.xmpp.register_plugin('xep_0060') # PubSub
        self.xmpp.register_plugin('xep_0199') # XMPP Ping

        self.xmpp.connect()
        #xmpp.process()   ## we are already async        

    def on_notify_service(self, ns, domain, service, data):
        """
        callback for service (from within appdaemon)
        """
        self._on_notify(data["message"])

    def on_notify_event(self, event_name, data, kwargs):
        """
        callback for event (from homeassistant)
        """
        self._on_notify(data["message"])

    def _on_notify(self, message):
        """
        send message to predefined XMPP contacts
        """
        for recipient in self.recipients:
            self.log("Sending '{}' to '{}'".format(message, recipient))
            self.xmpp.send_message_to(recipient, message)

    async def on_incoming_message(self, msg):
        message = msg["body"]
        sender = msg["from"]
        self.log("Incoming: '{}', from '{}".format(message, sender))

        # all commands are case insensitive
        message = message.lower()

        # find command, triggered by the incoming message, and run it
        command = Chatty.Command("", None)
        for x in self.commands:
            if message.startswith(x.name) and len(x.name) > len(command.name):
                command = x

        if command.name != "":
            self.log("Running command: {}".format(command.name))
            return await command.callback(message)
        else:
            self.log("Command not found.")
            return "Sorry, but... what?"

        # return an answer to the sender (optional)
        return None

    async def terminate(self):
        self.log("Terminating XMPP session")

        self.xmpp.do_reconnections = False
        self.xmpp.disconnect()

        await self.xmpp.disconnected
        del self.xmpp
                
        self.log("XMPP session terminated.")


    
class XMPPconnector(slixmpp.ClientXMPP):
    def __init__(self, jid, password, message_handler):
        slixmpp.ClientXMPP.__init__(self, jid, password)
        self.message_handler = message_handler
        self.log = message_handler.log

        self.do_reconnections = True
        self.is_first_connection = True

        self.add_event_handler("session_start", self.start)
        self.add_event_handler("message", self.on_message)
        self.add_event_handler("disconnected", self.on_disconnect)
        self.add_event_handler("connection_failed", self.on_connection_failure)

    def start(self, event):
        self.log("Connection established.")
        self.send_presence()
        self.get_roster()

        if self.is_first_connection:
            self.is_first_connection = False
        else:
            self.message_handler._on_notify("Reconnected after connection loss.")

    def on_disconnect(self, event):
        if self.do_reconnections:
            self.connect()

    def on_connection_failure(self, event):
        self.log("XMPP connection failed. Try to reconnect in 5min.")
        self.schedule("Reconnect after connection failure", 60*5, self.on_disconnect, event)

    def send_message_to(self, recipient, message):
        try:
            self.send_message(mto=recipient, mbody=message, mtype='chat')
        except slixmpp.xmlstream.xmlstream.NotConnectedError:
            self.log("Message NOT SENT, not connected.")
            ## TODO enqueue message for sending after reconnect
        except:
            self.log("Message NOT SENT, due to unexpected error!")

    async def on_message(self, msg):
        """
        called by slixmpp on incoming XMPP messages
        """

        if msg['type'] in ('chat', 'normal'):
            answer = await self.message_handler.on_incoming_message(msg)

            if answer:
                try:
                    msg.reply(answer).send()
                except slixmpp.xmlstream.xmlstream.NotConnectedError:
                    self.log("Reply NOT SENT, not connected.")
                    ## TODO enqueue message for sending after reconnect
                except:
                    self.log("Reply NOT SENT, due to unexpected error!")


class MyCommands:
    def __init__(self, chatty):
        """
        extend this class with your own commands
        """
        self.chatty = chatty

        self.rooms = ("küche", "wohnzimmer", "bad")
        self.room_to_heater = { "küche": "climate.heizung_kuche_mode",
                                "wohnzimmer": "climate.heizung_wohnzimmer_mode",
                                "bad": "climate.heizung_bad_mode"}

        chatty.register_command("help", self.help)
        chatty.register_command("heim", self.heim)
        chatty.register_command("weg", self.weg)
        chatty.register_command("heizung", self.heizung)
        chatty.register_command("h", self.heizung)
        chatty.register_command("auto", self.car)

    async def help(self, message):
        #return "I'm alive. But I can't really do anything, yet."
        return ("heim -> Heizungen hoch\n"
                "weg -> Heizungen runter\n"
                "h|heizung ZIMMER TEMP -> Heizung in ZIMMER auf TEMP\n"
                "auto -> Auto vorheizen")

    def _find_room(self, room):
        room = room.strip()
        for x in self.rooms:
            if x.startswith(room):
                return x

        return None

    def _parse_temp(self, temp):
        temp = temp.strip()
        if temp in ("an", "on"):
            return "on"
        if temp in ("aus", "off"):
            return "off"

        return temp.strip().replace(",", ".")

    def _set_temps(self, thermostats):
        for x in thermostats:
            room = x[0]
            heater_entity = self.room_to_heater[room]
            temp = x[1]
            self.chatty.log("Setting {} ({}) to {}".format(room, heater_entity, temp))

            if temp == "on":
                self.chatty.call_service("climate/turn_on", entity_id=heater_entity)
            elif temp == "off":
                self.chatty.call_service("climate/turn_off", entity_id=heater_entity)
            else:
                self.chatty.call_service("climate/set_temperature", entity_id=heater_entity, temperature=str(x[1]))

    async def _get_heater_setpoint(self, entity):
        return float(await self.chatty.get_state(entity_id=entity, attribute="temperature"))

    async def _get_is_temperature(self, entity):
        return float(await self.chatty.get_state(entity_id=entity, attribute="current_temperature"))
        
    async def _is_heating_on(self, entity):
        return False if await self.chatty.get_state(entity_id=entity) == "off" else True

    async def _query_heaters(self, rooms):
        results = []

        for room in rooms:
            heater = self.room_to_heater[room]
            if heater:
                values = [room]
                is_on = await self._is_heating_on(heater)
                if is_on:
                    values.append(await self._get_heater_setpoint(heater))
                else:
                    values.append("off")
                values.append(await self._get_is_temperature(heater))

                results.append(values)

        output = []
        for x in results:
            unit = "°C" if x[1] != "off" else ""
            output.append("{} [ {}{} | {}°C ]".format(x[0].capitalize(), x[1], unit, x[2]))

        return "\n".join(output)


    async def heim(self, message):
        heizungen = [
                        ("wohnzimmer", 22),
                        ("küche", 21),
                        ("bad", 21)
                    ]

        self._set_temps(heizungen)

        return "Wilkommen daheim. Heizungen sind bereit."

    async def weg(self, message):
        # heating down
        heizungen = [
                        ("wohnzimmer", 18),
                        ("küche", 18),
                        ("bad", 18)
                    ]

        self._set_temps(heizungen)


        # check windows

        w_bad = await self.chatty.get_state("binary_sensor.neo_coolcam_door_window_detector_sensor")
        w_wc = await self.chatty.get_state("binary_sensor.window_wc_virtual")
        w_mario = await self.chatty.get_state("binary_sensor.window_mario1")

        # warn if a window is left open
        if w_bad == "on" or w_mario == "on" or w_wc == "on":
            answer = "Achtung, Fenster noch offen! Bad: {}, WC: {}, Mario: {}".format(w_bad, w_wc, w_mario)
            answer += "\nHeizungen sind schonmal runter."
            return answer

        # everything is fine, goodby.
        return "Tschüss. Heizungen runter gedreht. Fenster sind zu."

    async def heizung(self, message):
        try:
            parts = message.strip().split(" ")

            # query all heaters
            if len(parts) == 1:
                return await self._query_heaters(self.rooms)

            room = self._find_room(parts[1])
            
            # query given heater
            if len(parts) == 2:
                return await self._query_heaters([room])
            
            # set given heater to new temp
            temp = self._parse_temp(parts[2])
            self._set_temps([(room, temp)])

            return "Setze {} auf {}{}".format(room, temp, "°C" if temp not in ("on", "off") else "")
        except:
            return "Sorry, I can't do that ..."

    async def car(self, message):
        self.chatty.call_service("switch/turn_on", entity_id="switch.leaf1youp_climate_control")
        return "Auto vorheizen (hoffentlich) gestartet."

