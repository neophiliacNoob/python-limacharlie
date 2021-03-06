from .Manager import Manager
from gevent.queue import Queue
import gevent.pool
import gevent
from gevent.lock import BoundedSemaphore
import uuid
import traceback
import json
import base64

class SpotCheck( object ):
    '''Representation of the process of looking for various Indicators of Compromise on the fleet.'''

    def __init__( self, oid, secret_api_key, cb_check, cb_on_start_check = None, cb_on_check_done = None, cb_on_offline = None, cb_on_error = None, n_concurrent = 1, n_sec_between_online_checks = 60, extra_params = {}, is_windows = True, is_linux = True, is_macos = True, tags = None ):
        '''Perform a check for specific characteristics on all hosts matching some parameters.

        Args:
            oid (uuid str): the Organization ID, if None, global credentials will be used.
            secret_api_key (str): the secret API key, if None, global credentials will be used.
            cb_check (func(Sensor)): callback function for every matching sensor, implements main check logic, returns True when check is finalized.
            cb_on_check_done (func(Sensor)): callback when a sensor is done with a check.
            cb_on_start_check (func(Sensor)): callback when a sensor is starting evaluation.
            cb_on_offline (func(Sensor)): callback when a sensor is offline so checking is delayed.
            cb_on_error (func(Sensor, stackTrace)): callback when an error occurs while checking a sensor.
            n_concurrent (int): number of sensors that should be checked concurrently, defaults to 1.
            n_sec_between_online_checks (int): number of seconds to wait between attempts to check a sensor that is offline, default to 60.
            is_windows (boolean): if True checks apply to Windows sensors, defaults to True.
            is_linux (boolean): if True checks apply to Linux sensors, defaults to True.
            is_macos (boolean): if True checks apply to MacOS sensors, defaults to True.
            tags (str): comma-seperated list of tags sensors to check must have.
        '''
        self._cbCheck = cb_check
        self._cbOnCheckDone = cb_on_check_done
        self._cbOnStartCheck = cb_on_start_check
        self._cbOnOffline = cb_on_offline
        self._cbOnError = cb_on_error
        self._nConcurrent = n_concurrent
        self._nSecBetweenOnlineChecks = n_sec_between_online_checks

        self._isWindows = is_windows
        self._isLinux = is_linux
        self._isMacos = is_macos

        self._tags = tags

        self._threads = gevent.pool.Group()
        self._stopEvent = gevent.event.Event()

        self._sensorsLeftToCheck = Queue()
        self._lock = BoundedSemaphore()
        self._pendingReCheck = 0

        self._lc = Manager( oid, secret_api_key, inv_id = 'spotcheck-%s' % str( uuid.uuid4() )[ : 4 ], is_interactive = True, extra_params = extra_params )

    def start( self ):
        '''Start the SpotCheck process, returns immediately.
        '''
        # We start by listing all the sensors in the org using paging.
        for sensor in self._lc.sensors():
            self._sensorsLeftToCheck.put( sensor )

        # Now that we have a list of sensors, we'll spawn n_concurrent spot checks,
        for _ in range( self._nConcurrent ):
            self._threads.add( gevent.spawn_later( 0, self._performSpotChecks ) )

        # Done, the threads will do the checks.

    def stop( self ):
        '''Stop the SpotCheck process, returns once activity has stopped.
        '''
        self._stopEvent.set()
        self._threads.join()

    def wait( self, timeout = None ):
        '''Wait for SpotCheck to be complete, or timeout occurs.

        Args:
            timeout (float): if specified, number of seconds to wait for SpotCheck to complete.

        Returns:
            True if SpotCheck is finished, False if a timeout was specified and reached before the SpotCheck is done.
        '''
        return self._threads.join( timeout = timeout )

    def _performSpotChecks( self ):
        while not self._stopEvent.wait( timeout = 0 ):
            try:
                sensor = self._sensorsLeftToCheck.get_nowait()
            except:
                # Check to see if some sensors are pending a re-check
                # after being offline.
                with self._lock:
                    # If there are no more sensors to check, we can exit.
                    if 0 == self._pendingReCheck and 0 == len( self._sensorsLeftToCheck ):
                        return
                gevent.sleep( 2 )
                continue

            # Check to see if the platform matches
            if self._isWindows is False or self._isLinux is False or self._isMacos is False:
                platform = sensor.getInfo()[ 'plat' ]
                if platform == 'windows' and not self._isWindows:
                    continue
                if platform == 'linux' and not self._isLinux:
                    continue
                if platform == 'macos' and not self._isMacos:
                    continue

            # If tags were set, check the sensor have them.
            if self._tags is not None:
                sensorTags = sensor.getTags()
                if not all( [ ( x in sensorTags ) for x in self._tags ] ):
                    continue

            # Check to see if the sensor is online.
            if not sensor.isOnline():
                if self._cbOnOffline is not None:
                    self._cbOnOffline( sensor )

                # Re-add it to sensors to check, after the timeout.
                def _doReCheck( s ):
                    with self._lock:
                        self._sensorsLeftToCheck.put( s )
                        self._pendingReCheck -= 1
                with self._lock:
                    self._pendingReCheck += 1
                self._threads.add( gevent.spawn_later( self._nSecBetweenOnlineChecks, _doReCheck, sensor ) )
                continue

            if self._cbOnStartCheck is not None:
                self._cbOnStartCheck( sensor )

            # By this point we have a sensor and it's likely online.
            try:
                result = self._cbCheck( sensor )
            except:
                # On errors, we notify the callback but assume any retry
                # is likely to also fail so we won't retry.
                if self._cbOnError is not None:
                    self._cbOnError( sensor, traceback.format_exc() )
                result = True
            if not result:
                # We assume the sensor was somehow offline.
                if self._cbOnOffline is not None:
                    self._cbOnOffline( sensor )
                # Re-add it to sensors to check, after the timeout.
                gevent.sleep( self._nSecBetweenOnlineChecks )
                self._sensorsLeftToCheck.put( sensor )
                continue

            # This means the check was done successfully.
            if self._cbOnCheckDone is not None:
                self._cbOnCheckDone( sensor )

if __name__ == "__main__":
    import argparse
    import getpass

    parser = argparse.ArgumentParser( prog = 'limacharlie.io spotcheck' )
    parser.add_argument( '-o', '--oid',
                         type = lambda x: str( uuid.UUID( x ) ),
                         required = False,
                         dest = 'oid',
                         help = 'the OID to authenticate as, if not specified global creds are used.' )
    parser.add_argument( '-n', '--n-concurrent',
                         type = int,
                         required = False,
                         default = 1,
                         dest = 'nConcurrent',
                         help = 'number of agents to spot-check concurrently.' )
    parser.add_argument( '--no-windows',
                         action = 'store_false',
                         default = True,
                         required = False,
                         dest = 'is_windows',
                         help = 'do NOT apply to Windows agents.' )
    parser.add_argument( '--no-linux',
                         action = 'store_false',
                         default = True,
                         required = False,
                         dest = 'is_linux',
                         help = 'do NOT apply to Linux agents.' )
    parser.add_argument( '--no-macos',
                         action = 'store_false',
                         default = True,
                         required = False,
                         dest = 'is_macos',
                         help = 'do NOT apply to MacOS agents.' )
    parser.add_argument( '--tags',
                         type = lambda x: [ _.strip().lower() for _ in x.split( ',' ) ],
                         required = False,
                         default = None,
                         dest = 'tags',
                         help = 'comma-seperated list of tags of the agents to check.' )
    parser.add_argument( '--extra-params',
                         type = lambda x: json.loads( x ),
                         required = False,
                         default = {},
                         dest = 'extra_params',
                         help = 'extra parameters to pass to the manager.' )
    parser.add_argument( '-f', '--file',
                         action = 'append',
                         required = False,
                         default = [],
                         dest = 'files',
                         help = 'file to look for.' )
    parser.add_argument( '-fp', '--file-pattern',
                         action = 'append',
                         nargs = 3,
                         required = False,
                         default = [],
                         dest = 'filepatterns',
                         help = 'takes 3 arguments, first is a directory, second is a file pattern like "*.exe", third is the depth of recursion in the directory.' )
    parser.add_argument( '-fh', '--file-hash',
                         action = 'append',
                         nargs = 4,
                         required = False,
                         default = [],
                         dest = 'filehashes',
                         help = 'takes 3 arguments, first is a directory, second is a file pattern like "*.exe", third is the depth of recursion in the directory and the fourth is the sha256 hash to look for.' )
    parser.add_argument( '-rk', '--registry-key',
                         action = 'append',
                         required = False,
                         default = [],
                         dest = 'registrykeys',
                         help = 'registry key to look for.' )
    parser.add_argument( '-rv', '--registry-value',
                         action = 'append',
                         nargs = 2,
                         required = False,
                         default = [],
                         dest = 'registryvalues',
                         help = 'takes 2 arguments, first is a registry key, second is the value to look for in the key.' )
    parser.add_argument( '-y', '--yara',
                         action = 'append',
                         required = False,
                         default = [],
                         dest = 'yarasystem',
                         help = 'yara signature file path to scan system-wide with (expensive).' )
    parser.add_argument( '-yf', '--yara-file',
                         action = 'append',
                         nargs = 4,
                         required = False,
                         default = [],
                         dest = 'yarafiles',
                         help = 'takes 4 arguments, first is a file path to yara signature, second is a directory, third is a file pattern (like "*.exe"), fourth is directory recursion depth.' )
    parser.add_argument( '-yp', '--yara-process',
                         action = 'append',
                         nargs = 2,
                         required = False,
                         default = [],
                         dest = 'yaraprocesses',
                         help = 'takes 2 arguments, first is a file path to yara signature, second is a process executable path pattern to scan memory and files.' )

    args = parser.parse_args()

    # Get creds if we need them.
    if args.oid is not None:
        secretApiKey = getpass.getpass( prompt = 'Enter secret API key: ' )
    else:
        secretApiKey = None

    def _genericSpotCheck( sensor ):
        global args

        for file in args.files:
            response = sensor.simpleRequest( 'file_info "%s"' % file.replace( '\\', '\\\\' ), timeout = 30 )
            if not response:
                raise Exception( 'timeout' )

            if 0 != response[ 'event' ].get( 'ERROR', 0 ):
                # File probably not found.
                continue

            # File was found.
            fileInfo = response[ 'event' ]

            # Try to ge the hash.
            response = sensor.simpleRequest( 'file_hash "%s"' % file.replace( '\\', '\\\\' ), timeout = 30 )
            if not response:
                raise Exception( 'timeout' )

            fileHash = None
            if 0 == response[ 'event' ].get( 'ERROR', 0 ):
                # We got a hash.
                fileHash = response[ 'event' ]

            _reportHit( sensor, { 'file_info' : fileInfo, 'file_hash' : fileHash } )

        for directory, filePattern, depth in args.filepatterns:
            response = sensor.simpleRequest( 'dir_list "%s" "%s" -d %s' % ( directory.replace( "\\", "\\\\" ), filePattern, depth ), timeout = 30 )
            if not response:
                raise Exception( 'timeout' )

            for entry in response[ 'event' ][ 'DIRECTORY_LIST' ]:
                _reportHit( sensor, { 'file_info' : entry } )

        for directory, filePattern, depth, hash in args.filehashes:
            if 64 != len( hash ):
                raise Exception( 'hash not valid sha256' )
            try:
                hash.decode( 'hex' )
            except:
                raise Exception( 'hash contains invalid characters' )
            response = sensor.simpleRequest( 'dir_find_hash "%s" "%s" -d %s --hash %s' % ( directory.replace( "\\", "\\\\" ), filePattern, depth , hash ), timeout = 3600 )
            if not response:
                raise Exception( 'timeout' )

            for entry in response[ 'event' ][ 'DIRECTORY_LIST' ]:
                _reportHit( sensor, { 'file_hash' : entry } )

        for regKey in args.registrykeys:
            response = sensor.simpleRequest( 'reg_list "%s"' % ( regKey.replace( '\\', '\\\\' ), ), timeout = 30 )
            if not response:
                raise Exception( 'timeout' )

            if 0 != response[ 'event' ][ 'ERROR' ]:
                # Registry probably not found.
                continue

            _reportHit( sensor, { 'reg_key' : response[ 'event' ] } )

        for regKey, regVal in args.registryvalues:
            response = sensor.simpleRequest( 'reg_list "%s"' % ( regKey.replace( '\\', '\\\\' ), ), timeout = 30 )
            if not response:
                raise Exception( 'timeout' )

            if 0 != response[ 'event' ][ 'ERROR' ]:
                # Registry probably not found.
                continue

            for valEntry in response[ 'event' ][ 'REGISTRY_VALUE' ]:
                if valEntry.get( 'NAME', '' ).lower() == regVal.lower():
                    _reportHit( sensor, { 'reg_key' : response[ 'event' ][ 'ROOT' ], 'reg_value' : valEntry } )

        for yaraSigFile in args.yarasystem:
            with open( yaraSigFile, 'rb' ) as f:
                yaraSig = base64.b64encode( f.read() )
            future = sensor.request( 'yara_scan %s' % ( yaraSig, ) )
            _handleYaraTasking( sensor, future )

        for yaraSigFile, directory, filePattern, depth in args.yarafiles:
            with open( yaraSigFile, 'rb' ) as f:
                yaraSig = base64.b64encode( f.read() )
            response = sensor.simpleRequest( 'dir_list "%s" "%s" -d %s' % ( directory.replace( "\\", "\\\\" ), filePattern, depth ), timeout = 30 )
            if not response:
                raise Exception( 'timeout' )
            for fileEntry in response[ 'event' ][ 'DIRECTORY_LIST' ]:
                filePath = fileEntry.get( 'FILE_PATH', None )
                if filePath is None:
                    continue
                future = sensor.request( 'yara_scan %s -f "%s"' % ( yaraSig, filePath.replace( "\\", "\\\\" ) ) )
                _handleYaraTasking( sensor, future )

        for yaraSigFile, procPattern in args.yaraprocesses:
            with open( yaraSigFile, 'rb' ) as f:
                yaraSig = base64.b64encode( f.read() )
            future = sensor.request( 'yara_scan %s -e %s' % ( yaraSig, procPattern.replace( '\\', '\\\\' ) ) )
            _handleYaraTasking( sensor, future )

        return True

    def _handleYaraTasking( sensor, future ):
        isDone = False
        while True:
            responses = future.getNewResponses( timeout = 3600 )
            if not responses:
                raise Exception( 'timeout' )
            for response in responses:
                if 'done' == response[ 'event' ].get( 'ERROR_MESSAGE', None ):
                    isDone = True
                    continue
                if 0 == response[ 'event' ].get( 'ERROR', 0 ):
                    # We got a hit, we don't care about individual hits right now.
                    _reportHit( sensor, { 'yara' : response[ 'event' ] } )
                else:
                    # Ignore if we failed to scan file.
                    pass

            if isDone:
                break

    def _reportHit( sensor, mtd ):
        print( "! (%s / %s): %s" % ( sensor, sensor.hostname(), json.dumps( mtd  ) ) )

    def _onError( sensor, error ):
        print( "X (%s / %s): %s" % ( sensor, sensor.hostname(), error ) )

    def _onOffline( sensor ):
        print( "? (%s / %s)" % ( sensor, sensor.hostname() ) )

    def _onDone( sensor ):
        print( ". (%s / %s)" % ( sensor, sensor.hostname() ) )

    def _onStartCheck( sensor ):
        print( "> (%s / %s)" % ( sensor, sensor.hostname() ) )

    checker = SpotCheck( args.oid,
                         secretApiKey,
                         _genericSpotCheck,
                         cb_on_start_check = _onStartCheck,
                         cb_on_check_done = _onDone,
                         cb_on_offline = _onOffline,
                         cb_on_error = _onError,
                         is_windows = args.is_windows,
                         is_linux = args.is_linux,
                         is_macos = args.is_macos,
                         tags = args.tags,
                         extra_params = args.extra_params )
    checker.start()
    checker.wait( 60 * 60 * 24 * 30 * 365 )