import copy
import hashlib
import json
import logging
import random
import time
from threading import Lock

import requests
from pgoapi import PGoApi
from pgoapi.auth_ptc import AuthPtc
from pgoapi.exceptions import AuthException, PgoapiError, \
    BannedAccountException, NoHashKeyException, NianticIPBannedException, NotLoggedInException
from pgoapi.protos.pogoprotos.inventory.item.item_id_pb2 import *
from pgoapi.utilities import get_cell_ids

from mrmime import _mr_mime_cfg, avatar, mrmime_pgpool_enabled
from mrmime.cyclicresourceprovider import CyclicResourceProvider
from mrmime.shadowbans import is_rareless_scan
from mrmime.utils import jitter_location, exception_caused_by_proxy_error

log = logging.getLogger(__name__)
login_lock = Lock()


class POGOAccount(object):
    def __init__(self, auth_service, username, password, hash_key=None,
                 hash_key_provider=None, proxy_url=None, proxy_provider=None):

        self.auth_service = auth_service
        self.username = username
        self.password = password

        # Get myself a copy of the config
        self.cfg = _mr_mime_cfg.copy()

        # Initialize hash keys
        self._hash_key = None
        if hash_key_provider and not hash_key_provider.is_empty():
            self._hash_key_provider = hash_key_provider
        elif hash_key:
            self._hash_key_provider = CyclicResourceProvider(hash_key)
            self._hash_key = hash_key
        else:
            self._hash_key_provider = None

        # Initialize proxies
        self._proxy_url = None
        if proxy_provider and not proxy_provider.is_empty():
            self._proxy_provider = proxy_provider
        elif proxy_url:
            self._proxy_provider = CyclicResourceProvider(proxy_url)
            self._proxy_url = proxy_url
        else:
            self._proxy_provider = None

        # Captcha
        self.captcha_url = None

        # Inventory information
        self.inbox = {}
        self.inventory = None
        self.inventory_balls = 0
        self.inventory_lures = 0
        self.inventory_total = 0
        self.incubators = []
        self.pokemon = {}
        self.eggs = []

        # Current location
        self.latitude = None
        self.longitude = None
        self.altitude = None

        # Count number of rareless scans (to detect shadowbans)
        self.rareless_scans = None
        self.shadowbanned = None

        # Last log message (for GUI/console)
        self.last_msg = ""

        # --- private fields

        self._reset_api()

        # Will be set to true if a request returns a BAD_REQUEST response which equals a ban
        self._bad_request_ban = False

        # Tutorial state and warn/ban flags
        self._player_state = {}

        # Trainer statistics
        self._player_stats = None

        # PGPool
        self._pgpool_auto_update_enabled = mrmime_pgpool_enabled() and self.cfg['pgpool_auto_update']
        self._last_pgpool_update = time.time()

        self.callback_egg_hatched = None

    def _reset_api(self):
        self._api = PGoApi(device_info=self._generate_device_info())
        self._download_settings_hash = None
        self._asset_time = 0
        self._item_templates_time = 0

        # Timestamp when last API request was made
        self._last_request = 0

        # Timestamp of last get_map_objects request
        self._last_gmo = self._last_request

        # Timestamp for incremental inventory updates
        self._last_timestamp_ms = None

        # Timestamp when previous user action is completed
        self._last_action = 0

    @property
    def hash_key(self):
        return self._hash_key

    @hash_key.setter
    def hash_key(self, new_key):
        if self._hash_key_provider is None:
            self._hash_key_provider = CyclicResourceProvider(new_key)
        else:
            self._hash_key_provider.set_single_resource(new_key)
        self._hash_key = new_key

    @property
    def proxy_url(self):
        return self._proxy_url

    @proxy_url.setter
    def proxy_url(self, new_proxy_url):
        if self._proxy_provider is None:
            self._proxy_provider = CyclicResourceProvider(new_proxy_url)
        else:
            self._proxy_provider.set_single_resource(new_proxy_url)
        self._proxy_url = new_proxy_url

    def set_position(self, lat, lng, alt):
        """Sets the location and altitude of the account"""
        self._api.set_position(lat, lng, alt)
        self.latitude = lat
        self.longitude = lng
        self.altitude = alt

    def release(self, reason="No longer in use"):
        if mrmime_pgpool_enabled():
            self.update_pgpool(release=True, reason=reason)

        self._api._session.close()

        auth_provider = self._api.get_auth_provider()
        if isinstance(auth_provider, AuthPtc):
            auth_provider._session.close()

        del self._api

        # Maybe delete more stuff too?
        # del self._player_state
        # del self._player_stats
        # del self.inventory
        # del self.incubators
        # del self.pokemon
        # del self.eggs

    def perform_request(self, add_main_request, buddy_walked=True, get_inbox=True, action=None):
        failures = 0
        while True:
            try:
                request = self._api.create_request()

                # Add main request
                add_main_request(request)

                # Standard requests with every call
                request.check_challenge()
                request.get_hatched_eggs()

                # Check inventory with correct timestamp
                if self._last_timestamp_ms:
                    request.get_holo_inventory(last_timestamp_ms=self._last_timestamp_ms)
                else:
                    request.get_holo_inventory()

                # Always check awarded badges
                request.check_awarded_badges()

                # Optional: download settings (with correct hash value)
                if self._download_settings_hash:
                    request.download_settings(hash=self._download_settings_hash)
                else:
                    request.download_settings()

                # Optional: request buddy kilometers
                if buddy_walked:
                    request.get_buddy_walked()

                if get_inbox:
                    request.get_inbox(is_history=True)

                return self._call_request(request, action)
            except NotLoggedInException as e:
                failures += 1
                if failures < 3:
                    self.log_warning("{}: Trying to reset API".format(repr(e)))
                    time.sleep(3)
                    self._reset_api()
                    self.set_position(self.latitude, self.longitude, self.altitude)
                    self.check_login()
                    time.sleep(1)
                else:
                    self.log_error("Failed {} times to reset API and repeat request. Giving up.".format(failures))
                    raise

    # Use API to check the login status, and retry the login if possible.
    def check_login(self):
        # Check auth ticket
        if self._api._auth_provider and self._api._auth_provider._access_token:
            remaining_time = self._api._auth_provider._access_token_expiry - time.time()
            if remaining_time > 60:
                self.log_debug('Credentials remain valid for another {} seconds.'.format(remaining_time))
                return True

        try:
            if not self.cfg['parallel_logins']:
                login_lock.acquire()

            # Set proxy if given.
            if self._proxy_provider:
                self._proxy_url = self._proxy_provider.next()
                self.log_debug("Using proxy {}".format(self._proxy_url))
                self._api.set_proxy({
                    'http': self._proxy_url,
                    'https': self._proxy_url
                })

            # Try to login. Repeat a few times, but don't get stuck here.
            num_tries = 0
            # One initial try + login_retries.
            while num_tries < self.cfg['login_retries']:
                try:
                    num_tries += 1
                    self.log_info("Login try {}.".format(num_tries))
                    if self._proxy_url:
                        self._api.set_authentication(
                            provider=self.auth_service,
                            username=self.username,
                            password=self.password,
                            proxy_config={
                                'http': self._proxy_url,
                                'https': self._proxy_url
                            })
                    else:
                        self._api.set_authentication(
                            provider=self.auth_service,
                            username=self.username,
                            password=self.password)
                    self.log_info("Login successful after {} tries.".format(num_tries))
                    break
                except AuthException as ex:
                    self.log_error(
                        'Failed to login. {} - Trying again in {} seconds.'.format(repr(ex),
                            self.cfg['login_delay']))
                    # Let the exception for the last try bubble up.
                    if num_tries >= self.cfg['login_retries']:
                        raise
                    time.sleep(self.cfg['login_delay'])

            if num_tries >= self.cfg['login_retries']:
                self.log_error(
                    'Failed to login in {} tries. Giving up.'.format(num_tries))
                return False

            if self.cfg['full_login_flow'] is True:
                try:
                    return self._initial_login_request_flow()
                except BannedAccountException:
                    self.log_warning("Account most probably BANNED! :-(((")
                    return False
                except CaptchaException:
                    self.log_warning("Account got CAPTCHA'd! :-|")
                    return False
                except Exception as e:
                    self.log_error("Login failed: {}".format(repr(e)))
                    return False
            return True
        finally:
            if not self.cfg['parallel_logins']:
                login_lock.release()

    def rotate_proxy(self):
        if self._proxy_provider:
            old_proxy = self._proxy_url
            self._proxy_url = self._proxy_provider.next()
            if self._proxy_url != old_proxy:
                self.log_info("Rotating proxy. Old: {}  New: {}".format(old_proxy, self._proxy_url))
                proxy_config = {
                    'http': self._proxy_url,
                    'https': self._proxy_url
                }
                self._api.set_proxy(proxy_config)
                self._api._auth_provider.set_proxy(proxy_config)

    def rotate_hash_key(self):
        # Set hash key for this request
        if not self._hash_key_provider:
            msg = "No hash key configured!"
            self.log_error(msg)
            raise NoHashKeyException()

        old_hash_key = self._hash_key
        self._hash_key = self._hash_key_provider.next()
        if self._hash_key != old_hash_key:
            self.log_debug("Using hash key {}".format(self._hash_key))
        self._api.activate_hash_server(self._hash_key)

    def is_logged_in(self):
        # Logged in? Enough time left? Cool!
        if self._api._auth_provider and self._api._auth_provider._access_token:
            remaining_time = self._api._auth_provider._access_token_expiry - time.time()
            return remaining_time > 60
        return False

    def is_warned(self):
        return self._player_state.get('warn')

    def is_banned(self):
        return self._bad_request_ban or self._player_state.get('banned', False)

    def has_captcha(self):
        return None if not self.is_logged_in() else (
            self.captcha_url and len(self.captcha_url) > 1)

    def uses_proxy(self):
        return self._proxy_url is not None and len(self._proxy_url) > 0

    def get_stats(self, attr, default=None):
        return getattr(self._player_stats, attr, default) if self._player_stats else default

    def get_state(self, key, default=None):
        return self._player_state.get(key, default)

    def needs_pgpool_update(self):
        return self._pgpool_auto_update_enabled and (
            time.time() - self._last_pgpool_update >= self.cfg['pgpool_update_interval'])

    def update_pgpool(self, release=False, reason=None):
        data = {
            'username': self.username,
            'password': self.password,
            'auth_service': self.auth_service,
            'system_id': self.cfg['pgpool_system_id'],
            'latitude': self.latitude,
            'longitude': self.longitude
        }
        # After login we know whether we've got a captcha
        if self.is_logged_in():
            data.update({
                'captcha': self.has_captcha()
            })
        if self.rareless_scans is not None:
            data['rareless_scans'] = self.rareless_scans
        if self.shadowbanned is not None:
            data['shadowbanned'] = self.shadowbanned
        if self._bad_request_ban:
            data['banned'] = True
        if self._player_state:
            data.update({
                'warn': self.is_warned(),
                'banned': self.is_banned(),
                'ban_flag': self.get_state('banned')
                #'tutorial_state': data.get('tutorial_state'),
            })
        if self._player_stats:
            data.update({
                'level': self.get_stats('level'),
                'xp': self.get_stats('experience'),
                'encounters': self.get_stats('pokemons_encountered'),
                'balls_thrown': self.get_stats('pokeballs_thrown'),
                'captures': self.get_stats('pokemons_captured'),
                'spins': self.get_stats('poke_stop_visits'),
                'walked': self.get_stats('km_walked')
            })
        if self.inventory:
            data.update({
                'balls': self.inventory_balls,
                'total_items': self.inventory_total,
                'pokemon': len(self.pokemon),
                'eggs': len(self.eggs),
                'incubators': len(self.incubators),
                'lures': self.inventory_lures
            })
        if self.inbox:
            data.update({
                'email': self.inbox.get('EMAIL'),
                'team': self.inbox.get('TEAM'),
                'coins': self.inbox.get('POKECOIN_BALANCE'),
                'stardust': self.inbox.get('STARDUST_BALANCE')
            })
        if release and reason:
            data['_release_reason'] = reason
        try:
            cmd = 'release' if release else 'update'
            url = '{}/account/{}'.format(self.cfg['pgpool_url'], cmd)
            r = requests.post(url, data=json.dumps(data))
            if r.status_code == 200:
                self.log_info("Successfully {}d PGPool account.".format(cmd))
            elif r.status_code == 503:
                self.log_warning(
                    "Could not update PGPool account: {} Try increasing 'pgpool_update_interval' in MrMime config.".format(r.content))
            else:
                self.log_warning("Got status {} from PGPool while updating account: {}".format(r.status_code, r.content))
        except Exception as e:
            self.log_error("Could not update PGPool account: {}".format(repr(e)))
        self._last_pgpool_update = time.time()

    def req_get_map_objects(self):
        """Scans current account location."""
        # Make sure that we don't hammer with GMO requests
        diff = self._last_gmo + self.cfg['scan_delay'] - time.time()
        if diff > 0:
            time.sleep(diff)

        # Jitter if wanted
        if self.cfg['jitter_gmo']:
            lat, lng = jitter_location(self.latitude, self.longitude)
        else:
            lat, lng = self.latitude, self.longitude

        cell_ids = get_cell_ids(lat, lng)
        timestamps = [0, ] * len(cell_ids)
        responses = self.perform_request(
            lambda req: req.get_map_objects(latitude=lat,
                                            longitude=lng,
                                            since_timestamp_ms=timestamps,
                                            cell_id=cell_ids),
            get_inbox=True
        )
        self._last_gmo = self._last_request

        return responses

    def req_encounter(self, encounter_id, spawn_point_id, latitude, longitude):
        return self.perform_request(lambda req: req.encounter(
            encounter_id=encounter_id,
            spawn_point_id=spawn_point_id,
            player_latitude=latitude,
            player_longitude=longitude), action=float(self.cfg['encounter_delay']))

    def req_catch_pokemon(self, encounter_id, spawn_point_id, ball,
                          normalized_reticle_size, spin_modifier):
        response = self.perform_request(lambda req: req.catch_pokemon(
            encounter_id=encounter_id,
            pokeball=ball,
            normalized_reticle_size=normalized_reticle_size,
            spawn_point_id=spawn_point_id,
            hit_pokemon=1,
            spin_modifier=spin_modifier,
            normalized_hit_position=1.0), action=6)

        if ('CATCH_POKEMON' in response):
            catch_pokemon = response['CATCH_POKEMON']
            catch_status = catch_pokemon.status
            capture_id = catch_pokemon.captured_pokemon_id

            # Determine caught Pokemon from inventory
            self.last_caught_pokemon = None
            if catch_status == 1:
                if capture_id in self.pokemon:
                    self.last_caught_pokemon = self.pokemon[capture_id]

        return response

    def req_release_pokemon(self, pokemon_id, pokemon_ids=None):
        return self.perform_request(
            lambda req: req.release_pokemon(pokemon_id=pokemon_id, pokemon_ids=pokemon_ids))

    def req_fort_details(self, fort_id, fort_lat, fort_lng):
        return self.perform_request(lambda req: req.fort_details(fort_id=fort_id,
                                                                 latitude=fort_lat,
                                                                 longitude=fort_lng), action=1.2)

    def req_fort_search(self, fort_id, fort_lat, fort_lng, player_lat,
                        player_lng):
        return self.perform_request(lambda req: req.fort_search(
            fort_id=fort_id,
            fort_latitude=fort_lat,
            fort_longitude=fort_lng,
            player_latitude=player_lat,
            player_longitude=player_lng), action=2)

    def req_add_fort_modifier(self, modifier_id, fort_id, player_lat, player_lng):
        response = self.perform_request(lambda req: req.add_fort_modifier(
            modifier_type=modifier_id,
            fort_id=fort_id,
            player_latitude=player_lat,
            player_longitude=player_lng), action=1.2)
        return response

    def req_gym_get_info(self, gym_id, gym_lat, gym_lng, player_lat, player_lng):
        return self.perform_request(
            lambda req: req.gym_get_info(gym_id=gym_id,
                                         player_lat_degrees=player_lat,
                                         player_lng_degrees=player_lng,
                                         gym_lat_degrees=gym_lat,
                                         gym_lng_degrees=gym_lng))

    def req_recycle_inventory_item(self, item_id, amount):
        return self.perform_request(lambda req: req.recycle_inventory_item(
            item_id=item_id,
            count=amount), action=2)

    def req_level_up_rewards(self, level):
        return self.perform_request(
            lambda req: req.level_up_rewards(level=level))

    def req_verify_challenge(self, captcha_token):
        responses = self.perform_request(lambda req: req.verify_challenge(token=captcha_token), action=4)
        if 'VERIFY_CHALLENGE' in responses:
            response = responses['VERIFY_CHALLENGE']
            if response.HasField('success'):
                self.captcha_url = None
                self.log_info("Successfully uncaptcha'd.")
                return True
            else:
                self.log_warning("Failed verifyChallenge")
                return False

    def req_use_item_egg_incubator(self, incubator_id, egg_id):
        return self.perform_request(
            lambda req: req.use_item_egg_incubator(
                item_id=incubator_id,
                pokemon_id=egg_id))

    def seq_spin_pokestop(self, fort_id, fort_lat, fort_lng, player_lat,
                          player_lng):
        # We first need to tap the Pokestop before we can spin it, so it's a sequence of actions
        self.req_fort_details(fort_id, fort_lat, fort_lng)
        return self.req_fort_search(fort_id, fort_lat, fort_lng, player_lat, player_lng)

    # =======================================================================

    def _generate_device_info(self):
        identifier = self.username + self.password
        md5 = hashlib.md5()
        md5.update(identifier.encode('utf-8'))
        pick_hash = int(md5.hexdigest(), 16)

        iphones = {
            'iPhone6,1': 'N51AP',
            'iPhone6,2': 'N53AP',
            'iPhone7,1': 'N56AP',
            'iPhone7,2': 'N61AP',
            'iPhone8,1': 'N71AP',
            'iPhone8,2': 'N66AP',
            'iPhone8,4': 'N69AP',
            'iPhone9,1': 'D10AP',
            'iPhone9,2': 'D11AP',
            'iPhone9,3': 'D101AP',
            'iPhone9,4': 'D111AP',
            'iPhone10,1': 'D20AP',
            'iPhone10,2': 'D21AP',
            'iPhone10,3': 'D22AP',
            'iPhone10,4': 'D201AP',
            'iPhone10,5': 'D211AP',
            'iPhone10,6': 'D221AP'
        }

        
        ios11 = ('11.0.1', '11.0.2', '11.0.3', '11.1', '11.1.1', '11.1.2', '11.2', '11.2.1', '11.2.2', '11.2.5', '11.2.6')

        device_info = {
            'device_brand': 'Apple',
            'device_model': 'iPhone',
            'hardware_manufacturer': 'Apple',
            'firmware_brand': 'iPhone OS'
        }

        devices = tuple(iphones.keys())
        device = devices[pick_hash % len(devices)]
        device_info['device_model_boot'] = device
        device_info['hardware_model'] = iphones[device]
        device_info['device_id'] = md5.hexdigest()

        ios_pool = ios11
        device_info['firmware_type'] = ios_pool[pick_hash % len(ios_pool)]

        self.log_debug("Using an {} on iOS {} with device ID {}".format(device,
                                                                        device_info['firmware_type'],
                                                                        device_info['device_id']))

        return device_info

    def _call_request(self, request, action=None):
        # Wait until a previous user action gets completed
        if action:
            now = time.time()
            # wait for the time required, or at least a configured min_request_delay (default 0.5)
            min_sleep = float(self.cfg['min_request_delay'])
            if self._last_action > now + min_sleep:
                time.sleep(self._last_action - now)
            else:
                time.sleep(min_sleep)

        req_method_list = copy.deepcopy(request._req_method_list)

        response = {}
        rotate_proxy = False
        while True:
            try:
                self.rotate_hash_key()
                if rotate_proxy:
                    self.rotate_proxy()
                    rotate_proxy = False

                response = request.call(use_dict=False)

                self._last_request = time.time()
                break
            except NianticIPBannedException as ex:
                if not self.uses_proxy():
                    # IP banned and not using proxies... we should quit
                    self.log_error(repr(ex))
                    sys.exit(ex)
            except NotLoggedInException:
                # We need to re-login and re-post the request but we do it one level up
                raise
            except PgoapiError as ex:
                defaultRetryDelay = float(self.cfg['request_retry_delay'])
                # Rotate proxy if it's part of the error
                if self.uses_proxy() and exception_caused_by_proxy_error(ex):
                    rotate_proxy = True
                    # Shorten retry delay according to number of available proxies
                    retryDelay = defaultRetryDelay / self._proxy_provider.len()
                else:
                    # Shorten retry delay according to number of available hash keys
                    retryDelay = defaultRetryDelay / self._hash_key_provider.len()
                self.log_warning("{}: Retrying in {:.1f}s.".format(repr(ex), retryDelay))
                time.sleep(retryDelay)
            except Exception as ex:
                # No PgoapiError - this is serious!
                raise

        if not 'envelope' in response:
            msg = 'No response envelope. Something is wrong!'
            self.log_warning(msg)
            raise PgoapiError(msg)

        # status_code 3 means BAD_REQUEST, so probably banned
        status_code = response['envelope'].status_code
        if status_code == 3:
            log_suffix = ''
            if self.cfg['dump_bad_requests']:
                with open('BAD_REQUESTS.txt', 'a') as f:
                    f.write(repr(req_method_list))
                    f.close()
                log_suffix = ' Dumped request to BAD_REQUESTS.txt.'
            self.log_warning("Got BAD_REQUEST response. Possible Ban!{}".format(log_suffix))
            self._bad_request_ban = True
            raise BannedAccountException

        # Clean up
        del response['envelope']

        if not 'responses' in response:
            self.log_error("Got no responses at all!")
            return {}

        # Set the timer when the user action will be completed
        if action:
            self._last_action = self._last_request + action

        # Return only the responses
        responses = response['responses']

        self._parse_responses(responses)

        if self.needs_pgpool_update():
            self.update_pgpool()

        return responses

    def _update_inventory_totals(self):
        ball_ids = [
            ITEM_POKE_BALL,
            ITEM_GREAT_BALL,
            ITEM_ULTRA_BALL,
            ITEM_MASTER_BALL
        ]
        lure_ids = [
            ITEM_TROY_DISK
        ]
        balls = 0
        lures = 0
        total_items = 0
        for item_id in self.inventory:
            if item_id in ball_ids:
                balls += self.inventory[item_id]
            if item_id in lure_ids:
                lures += self.inventory[item_id]
            total_items += self.inventory[item_id]
        self.inventory_balls = balls
        self.inventory_lures = lures
        self.inventory_total = total_items

    def _parse_responses(self, responses):
        for response_type in responses.keys():
            response = responses[response_type]

            if response_type == 'GET_INBOX':
                self._parse_inbox_response(response)
                del responses[response_type]

            elif response_type == 'GET_HOLO_INVENTORY':
                api_inventory = response

                # Set an (empty) inventory if necessary
                if self.inventory is None:
                    self.inventory = {}

                # Update inventory (balls, items)
                self._parse_inventory_delta(api_inventory)
                self._update_inventory_totals()

                # Update last timestamp for inventory requests
                self._last_timestamp_ms = api_inventory.inventory_delta.new_timestamp_ms

                # Clean up
                del responses[response_type]

            # Get settings hash from response for future calls
            elif response_type == 'DOWNLOAD_SETTINGS':
                if response.hash:
                    self._download_settings_hash = response.hash
                # TODO: Check forced client version and exit program if different

                # Clean up
                del responses[response_type]

            elif response_type == 'GET_PLAYER':
                self._player_state = {
                    'tutorial_state': response.player_data.tutorial_state,
                    'buddy': response.player_data.buddy_pokemon.id,
                    'warn': response.warn,
                    'banned': response.banned
                }

                # Clean up
                del responses[response_type]

                if self._player_state['banned']:
                    self.log_warning("GET_PLAYER has the 'banned' flag set.")
                    raise BannedAccountException

            # Check for captcha
            elif response_type == 'CHECK_CHALLENGE':
                self.captcha_url = response.challenge_url

                # Clean up
                del responses[response_type]

                if self.has_captcha() and self.cfg['exception_on_captcha']:
                    raise CaptchaException

            elif response_type == 'GET_MAP_OBJECTS':
                if is_rareless_scan(response):
                    if self.rareless_scans is None:
                        self.rareless_scans = 1
                    else:
                        self.rareless_scans += 1
                else:
                    self.rareless_scans = 0

            elif response_type == 'GET_HATCHED_EGGS':
                if self.callback_egg_hatched and response.success and len(response.hatched_pokemon) > 0:
                    for i in range(0, len(response.pokemon_id)):
                        hatched_egg = {
                            'experience_awarded': response.experience_awarded[i],
                            'candy_awarded': response.candy_awarded[i],
                            'stardust_awarded': response.stardust_awarded[i],
                            'egg_km_walked': response.egg_km_walked[i],
                            'hatched_pokemon': response.hatched_pokemon[i]
                        }
                        self.callback_egg_hatched(self, hatched_egg)

    def _parse_inbox_response(self, response):
        vars = response.inbox.builtin_variables
        for v in vars:
            if v.name in ('POKECOIN_BALANCE', 'STARDUST_BALANCE'):
                self.inbox[v.name] = int(v.literal)
            elif v.name == 'EMAIL':
                self.inbox[v.name] = v.literal
            elif v.name == 'TEAM':
                self.inbox[v.name] = v.key

    def _parse_inventory_delta(self, inventory):
        for item in inventory.inventory_delta.inventory_items:
            item_data = item.inventory_item_data
            if item_data.HasField('player_stats'):
                self._player_stats = item_data.player_stats
            elif item_data.HasField('item'):
                item_id = item_data.item.item_id
                item_count = item_data.item.count
                self.inventory[item_id] = item_count
            elif item_data.HasField('egg_incubators'):
                incubators = item_data.egg_incubators.egg_incubator
                for incubator in incubators:
                    if incubator.pokemon_id == 0:
                        self.incubators.append({
                            'id': incubator.id,
                            'item_id': incubator.item_id,
                            'uses_remaining': incubator.uses_remaining
                        })
            elif item_data.HasField('pokemon_data'):
                p_data = item_data.pokemon_data
                p_id = p_data.id
                if not p_data.is_egg:
                    self.pokemon[p_id] = {
                        'pokemon_id': p_data.pokemon_id,
                        'move_1': p_data.move_1,
                        'move_2': p_data.move_2,
                        'individual_attack': p_data.individual_attack,
                        'individual_defense': p_data.individual_defense,
                        'individual_stamina': p_data.individual_stamina,
                        'height': p_data.height_m,
                        'weight': p_data.weight_kg,
                        'costume': p_data.pokemon_display.costume,
                        'form': p_data.pokemon_display.form,
                        'gender': p_data.pokemon_display.gender,
                        'shiny': p_data.pokemon_display.shiny,
                        'cp': p_data.cp,
                        'cp_multiplier': p_data.cp_multiplier,
                        'is_bad': p_data.is_bad
                    }
                else:
                    # Incubating egg
                    if p_data.egg_incubator_id:
                        continue
                    # Egg
                    self.eggs.append({
                        'id': p_id,
                        'km_target': p_data.egg_km_walked_target
                    })

    def _initial_login_request_flow(self):
        self.log_info("Performing full login flow requests")

        # Empty request -----------------------------------------------------
        self.log_debug("Login Flow: Empty request")
        # ===== empty
        request = self._api.create_request()
        self._call_request(request)
        time.sleep(random.uniform(.43, .97))

        # Get player info ---------------------------------------------------
        self.log_debug("Login Flow: Get player state")
        # ===== GET_PLAYER (unchained)
        request = self._api.create_request()
        request.get_player(
            player_locale=self.cfg['player_locale'])
        self._call_request(request)
        time.sleep(random.uniform(.53, 1.1))

        # Download remote config --------------------------------------------
        self.log_debug("Login Flow: Downloading remote config")
        asset_time, template_time = self._download_remote_config_version()
        time.sleep(1)

        # Assets and item templates -----------------------------------------
        if self.cfg['download_assets_and_items'] and asset_time > self._asset_time:
            self.log_debug("Login Flow: Download asset digest")
            self._get_asset_digest(asset_time)
        else:
            self.log_debug("Login Flow: Skipping asset digest download")

        if self.cfg['download_assets_and_items'] and template_time > self._item_templates_time:
            self.log_debug("Login Flow: Download item templates")
            self._download_item_templates(template_time)
        else:
            self.log_debug("Login Flow: Skipping item template download")

        # TODO: Maybe download translation URLs from assets? Like pogonode?

        # Checking tutorial -------------------------------------------------
        if (self._player_state['tutorial_state'] is not None and
                not all(x in self._player_state['tutorial_state'] for x in
                        (0, 1, 3, 4, 7))):
            self.log_info("Completing tutorial")
            self._complete_tutorial()
        else:
            # Get player profile
            self.log_debug("Login Flow: Get player profile")
            # ===== GET_PLAYER_PROFILE
            self.perform_request(lambda req: req.get_player_profile())
            time.sleep(random.uniform(.2, .3))

        # Level up rewards --------------------------------------------------
        self.log_debug("Login Flow: Get levelup rewards")
        # ===== LEVEL_UP_REWARDS
        self.perform_request(lambda req: req.level_up_rewards(level=self._player_stats.level))

        # Check store -------------------------------------------------------
        # TODO: There is currently no way to call the GET_STORE_ITEMS platform request.

        self.log_info('After-login procedure completed.')
        time.sleep(random.uniform(.5, 1.3))
        return True

    def _set_avatar(self, tutorial=False):
        player_avatar = avatar.new()
        # ===== LIST_AVATAR_CUSTOMIZATIONS
        self.perform_request(lambda req: req.list_avatar_customizations(
            avatar_type=player_avatar['avatar'],
            # slot=tuple(),
            filters=2), buddy_walked=not tutorial, action=5, get_inbox=False)
        time.sleep(random.uniform(7, 14))

        # ===== SET_AVATAR
        self.perform_request(
            lambda req: req.set_avatar(player_avatar=player_avatar),
            buddy_walked=not tutorial, action=2, get_inbox=False)

        if tutorial:
            time.sleep(random.uniform(.5, 4))

            # ===== MARK_TUTORIAL_COMPLETE
            self.perform_request(
                lambda req: req.mark_tutorial_complete(
                    tutorials_completed=1), buddy_walked=False, get_inbox=False)

            time.sleep(random.uniform(.5, 1))

        self.perform_request(
            lambda req: req.get_player_profile(), action=1, get_inbox=False)

    def _complete_tutorial(self):
        tutorial_state = self._player_state['tutorial_state']
        if 0 not in tutorial_state:
            # legal screen
            self.log_debug("Tutorial #0: Legal screen")
            # ===== MARK_TUTORIAL_COMPLETE
            self.perform_request(lambda req: req.mark_tutorial_complete(
                tutorials_completed=0), buddy_walked=False, get_inbox=False)
            time.sleep(random.uniform(.35, .525))

            # ===== GET_PLAYER
            self.perform_request(
                lambda req: req.get_player(
                    player_locale=self.cfg['player_locale']),
                buddy_walked=False, get_inbox=False)
            time.sleep(1)

        if 1 not in tutorial_state:
            # avatar selection
            self.log_debug("Tutorial #1: Avatar selection")
            self._set_avatar(tutorial=True)

        starter_id = None
        if 3 not in tutorial_state:
            # encounter tutorial
            self.log_debug("Tutorial #3: Catch starter Pokemon")
            time.sleep(random.uniform(.7, .9))
            # ===== GET_DOWNLOAD_URLS
            self.perform_request(lambda req: req.get_download_urls(asset_id=
                                                                   [
                                                                       '1a3c2816-65fa-4b97-90eb-0b301c064b7a/1487275569649000',
                                                                       'aa8f7687-a022-4773-b900-3a8c170e9aea/1487275581132582',
                                                                       'e89109b0-9a54-40fe-8431-12f7826c8194/1487275593635524']),
                                 get_inbox=False)

            time.sleep(random.uniform(7, 10.3))
            starter = random.choice((1, 4, 7))
            # ===== ENCOUNTER_TUTORIAL_COMPLETE
            self.perform_request(lambda req: req.encounter_tutorial_complete(
                pokemon_id=starter), action=1, get_inbox=False)

            time.sleep(random.uniform(.4, .5))
            # ===== GET_PLAYER
            responses = self.perform_request(
                lambda req: req.get_player(player_locale=self.cfg['player_locale']), get_inbox=False)

            try:
                inventory = responses[
                    'GET_HOLO_INVENTORY'].inventory_delta.inventory_items
                for item in inventory:
                    pokemon = item.inventory_item_data.pokemon_data
                    if pokemon.id:
                        starter_id = pokemon.id
                        break
            except (KeyError, TypeError):
                starter_id = None

        if 4 not in tutorial_state:
            # name selection
            self.log_debug("Tutorial #4: Set trainer name")
            time.sleep(random.uniform(12, 18))
            # ===== CLAIM_CODENAME
            self.perform_request(
                lambda req: req.claim_codename(codename=self.username),
                action=2, get_inbox=False)

            time.sleep(.7)
            # ===== GET_PLAYER
            self.perform_request(
                lambda req: req.get_player(player_locale=self.cfg['player_locale']), get_inbox=False)
            time.sleep(.13)

            # ===== MARK_TUTORIAL_COMPLETE
            self.perform_request(lambda req: req.mark_tutorial_complete(
                tutorials_completed=4), buddy_walked=False, get_inbox=False)

        if 7 not in tutorial_state:
            # first time experience
            self.log_debug("Tutorial #7: First time experience")
            time.sleep(random.uniform(3.9, 4.5))
            # ===== MARK_TUTORIAL_COMPLETE
            self.perform_request(lambda req: req.mark_tutorial_complete(
                tutorials_completed=7), get_inbox=False)

        # set starter as buddy
        if starter_id:
            self.log_debug("Setting buddy Pokemon")
            time.sleep(random.uniform(4, 5))
            # ===== SET_BUDDY_POKEMON
            self.perform_request(
                lambda req: req.set_buddy_pokemon(pokemon_id=starter_id),
                action=2, get_inbox=False)
            time.sleep(random.uniform(.8, 1.2))

        time.sleep(.2)
        return True

    def _download_remote_config_version(self):
        # ===== DOWNLOAD_REMOTE_CONFIG_VERSION
        device_model_boot = self._generate_device_info()
        responses = self.perform_request(lambda req: req.download_remote_config_version(
                                                platform=1,
                                                device_model=device_model_boot['device_model_boot'],
                                                app_version=PGoApi.get_api_version()),
                                                buddy_walked=False, get_inbox=False)
        if 'DOWNLOAD_REMOTE_CONFIG_VERSION' not in responses:
            raise Exception("Call to download_remote_config_version did not"
                            " return proper response.")
        remote_config = responses['DOWNLOAD_REMOTE_CONFIG_VERSION']
        return remote_config.asset_digest_timestamp_ms / 1000000, \
               remote_config.item_templates_timestamp_ms / 1000

    def _get_asset_digest(self, asset_time):
        i = random.randint(0, 3)
        result = 2
        page_offset = 0
        page_timestamp = 0
        while result == 2:
            # ===== GET_ASSET_DIGEST
            responses = self.perform_request(lambda req: req.get_asset_digest(
                platform=1,
                app_version=PGoApi.get_api_version(),
                paginate=True,
                page_offset=page_offset,
                page_timestamp=page_timestamp), buddy_walked=False, get_inbox=False)
            if i > 2:
                time.sleep(1.45)
                i = 0
            else:
                i += 1
                time.sleep(.2)
            try:
                response = responses['GET_ASSET_DIGEST']
            except KeyError:
                break
            result = response.result
            page_offset = response.page_offset
            page_timestamp = response.timestamp_ms
        self._asset_time = asset_time

    def _download_item_templates(self, template_time):
        i = random.randint(0, 3)
        result = 2
        page_offset = 0
        page_timestamp = 0
        while result == 2:
            # ===== DOWNLOAD_ITEM_TEMPLATES
            responses = self.perform_request(lambda req: req.download_item_templates(
                paginate=True,
                page_offset=page_offset,
                page_timestamp=page_timestamp), buddy_walked=False, get_inbox=False)
            if i > 2:
                time.sleep(1.5)
                i = 0
            else:
                i += 1
                time.sleep(.25)
            try:
                response = responses['DOWNLOAD_ITEM_TEMPLATES']
            except KeyError:
                break
            result = response.result
            page_offset = response.page_offset
            page_timestamp = response.timestamp_ms
        self._item_templates_time = template_time

    def log_info(self, msg):
        self.last_msg = msg
        log.info(u"[{}] {}".format(self.username, msg))

    def log_debug(self, msg):
        self.last_msg = msg
        log.debug(u"[{}] {}".format(self.username, msg))

    def log_warning(self, msg):
        self.last_msg = msg
        log.warning(u"[{}] {}".format(self.username, msg))

    def log_error(self, msg):
        self.last_msg = msg
        log.error(u"[{}] {}".format(self.username, msg))


class CaptchaException(PgoapiError):
    """Raised when an account got captcha'd"""
