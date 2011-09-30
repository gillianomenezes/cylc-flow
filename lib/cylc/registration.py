#!/usr/bin/env python

#C: THIS FILE IS PART OF THE CYLC FORECAST SUITE METASCHEDULER.
#C: Copyright (C) 2008-2011 Hilary Oliver, NIWA
#C:
#C: This program is free software: you can redistribute it and/or modify
#C: it under the terms of the GNU General Public License as published by
#C: the Free Software Foundation, either version 3 of the License, or
#C: (at your option) any later version.
#C:
#C: This program is distributed in the hope that it will be useful,
#C: but WITHOUT ANY WARRANTY; without even the implied warranty of
#C: MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#C: GNU General Public License for more details.
#C:
#C: You should have received a copy of the GNU General Public License
#C: along with this program.  If not, see <http://www.gnu.org/licenses/>.

import pickle
import datetime, time
import os, sys, re
from conf.CylcGlobals import central_regdb_dir, local_regdb_dir
from config import config, SuiteConfigError

delimiter = '.'
delimiter_re = '\.'

# NOTE:ABSPATH (see below)
#   dir = os.path.abspath( dir )
# On GPFS os.path.abspath() returns the full path with fileset
# prefix which can make filenames (for files stored under the 
# cylc suite directory) too long for hardwired limits in the
# UM, which then core dumps. Manual use of $PWD to absolutize a relative
# path, on GPFS, results in a shorter string ... so I use this for now.

        ## cheap suite title extraction WAS USED IN refresh()
        #try:
        #    dir, title = self.items[suite]
        #except KeyError:
        #    raise SuiteNotFoundError, suite
        #file = os.path.join( dir, 'suite.rc' )
        #if not os.path.isfile( file ):
        #    raise RegistrationError, 'File not found: ' + file
        #
        #found = False
        #for line in open( file, 'rb' ):
        #    m = re.match( '^\s*title\s*=\s*(.*)$', line )
        #    if m:
        #        title = m.groups()[0]
        #        # strip trailing space
        #        title = title.rstrip()
        #        # NOTE: ANY TRAILING COMMENT WILL BE INCLUDED IN THE TITLE
        #        #     (but this doesn't really matter for our purposes)
        #        # (stripping isn't trivial in general - what about strings?)
        #        found = True
        #        break
        #
        #if not found:
        #    print >> sys.stderr, "WARNING: title not found by simple search, for " + suite
        #    # This means the title is defined in a suite.rc include-file, or
        #    # is not defined. In the latter case, a full parse will result
        #    # in the default title being used (from conf/suiterc.spec). 
 
class RegistrationError( Exception ):
    """
    Attributes:
        message - what the problem is. 
    """
    def __init__( self, msg ):
        self.msg = msg
    def __str__( self ):
        return repr(self.msg)

class InvalidFilterError( RegistrationError ):
    def __init__( self, regfilter ):
        self.msg = "ERROR, Invalid filter expression: " + regfilter

class SuiteNotFoundError( RegistrationError ):
    def __init__( self, suite ):
        self.msg = "ERROR, suite not found: " + suite

class SuiteTitleNotFoundError( RegistrationError ):
    def __init__( self, suite ):
        self.msg = "ERROR, suite title not found by simple parsing: " + suite

class SuiteOrGroupNotFoundError( RegistrationError ):
    def __init__( self, sog ):
        self.msg = "ERROR, suite or group not found: " + sog

class SuiteTakenError( RegistrationError ):
    def __init__( self, suite, owner=None ):
        self.msg = "ERROR: " + suite + " is already a registered suite."
        if owner:
            self.msg += ' (' + owner + ')'

class NotAGroupError( RegistrationError ):
    def __init__( self, reg ):
        self.msg = "ERROR: " + reg + " is a registered suite, not a group."

class IsAGroupError( RegistrationError ):
    def __init__( self, reg ):
        self.msg = "ERROR: " + reg + " is already a registered group."

class SuiteNotRegisteredError( RegistrationError ):
    def __init__( self, suite ):
        self.msg = "ERROR: Suite not found " + suite

class GroupNotFoundError( RegistrationError ):
    def __init__( self, group, owner=None ):
        self.msg = "ERROR: group not found " + group
        if owner:
            self.msg += ' (' + owner + ')'

class GroupAlreadyExistsError( RegistrationError ):
    def __init__( self, group, owner=None ):
        self.msg = "ERROR: group already exists " + group
        if owner:
            self.msg += ' (' + owner + ')'

class RegistrationNotValidError( RegistrationError ):
    pass

class DatabaseLockedError( RegistrationError ):
    pass

class OwnerError( RegistrationError ):
    pass

class RegPathError( RegistrationError ):
    def __init__( self, reg ):
        self.msg = "ERROR, illegal registration path: " + reg

class dbgetter:
    # Use to get unaliased suite registration data from an input registration.
    def __init__( self, central=False, verbose=False ):
        if central:
            self.db = centraldb(verbose=verbose)
        else:
            self.db = localdb(verbose=verbose)
    def get_suite( self, reg ):
        self.db.load_from_file()
        suite = self.db.unalias( reg )
        suiterc = self.db.getrc( suite )
        return suite, suiterc

class regdb(object):
    """
    A simple suite registration database.
    """
    def __init__( self, dir, file, verbose=False ):
        self.verbose = verbose
        self.user = os.environ['USER']
        self.dir = dir
        self.file = file
        # items[one][two]...[name] = (dir,title)
        self.items = {}
        # create initial database directory if necessary
        if not os.path.exists( self.dir ):
            try:
                os.makedirs( self.dir )
            except Exception,x:
                print >> sys.stderr, "ERROR, failed to create directory:", self.dir
                print >> sys.stderr, x
                sys.exit(1)
        self.mtime_at_load = None
        self.lockfile = os.path.join( self.dir, 'lock' )
        self.statehash = None

    def get_hash(self):
        return hash( str(sorted(self.items.items())))

    def lock( self ):
        if os.path.exists( self.lockfile ):
            print >> sys.stderr, "lock file:", self.lockfile
            raise DatabaseLockedError, 'ERROR: ' + self.file + ' is locked'
        if self.verbose:
            print "   (locking database " + self.file + ")"
        lockfile = open( self.lockfile, 'wb' )
        lockfile.write( 'locked by ' + self.user + '\n' )
        lockfile.write( str(datetime.datetime.now()))
        lockfile.close()

    def unlock( self ):
        if os.path.exists( self.lockfile ):
            if self.verbose:
                print "   (unlocking database " + self.file + ")"
            try:
                os.unlink( self.lockfile )
            except OSError, x:
                raise

    def changed_on_disk( self ):
        # use to detect ONE change in database since we read it,
        # while we have read-only access.
        try:
            st_mtime = os.stat( self.file ).st_mtime 
        except OSError:
            # file not found => no suites registered.
            return False

        if st_mtime != self.mtime_at_load:
            return True
        else:
            return False
        
    def load_from_file( self ):
        if self.verbose:
            print "LOADING " + self.file
        try:
            self.mtime_at_load = os.stat(self.file).st_mtime
        except OSError:
            # no file: no suites registered  yet
            self.mtime_at_load = time.time()
            return
        input = open( self.file, 'rb' )
        try:
            self.items = pickle.load( input )
        except Exception, x:
            input.close()
            raise RegistrationError, 'ERROR: failed to read database, ' + self.file
        input.close()
        # record state at load
        self.statehash = self.get_hash()

    def dump_to_file( self ):
        newhash = self.get_hash()
        if newhash != self.statehash:
            if self.verbose:
                print "REWRITING DATABASE"
            output = open( self.file, 'w' )
            pickle.dump( self.items, output )
            output.close()
            self.statehash = newhash
        else:
            if self.verbose:
                print "   (database unchanged)"

    def register( self, suite, dir ):
        for key in self.items.keys():
            if key == suite:
                raise SuiteTakenError, suite
            elif key.startswith(suite + delimiter ):
                raise IsAGroupError, suite
            elif suite.startswith(key + delimiter ):
                raise NotAGroupError, key

        if dir.startswith( '->' ):
            # (alias: dir points to target suite reg)
            title = self.get_suite_title( dir[2:] )
            # use the lowest level alias
            target = self.unalias( dir[2:] )
            dir = '->' + target
        else:
            # Remove trailing '/'
            dir = dir.rstrip( '/' )
            # Remove leading './'
            dir = re.sub( '^\.\/', '', dir )
            # Make registered path absolute # see NOTE:ABSPATH above
            if not re.search( '^/', dir ):
                dir = os.path.join( os.environ['PWD'], dir )
            title = self.get_suite_title( suite, path=dir )

        #if self.verbose:
        print 'REGISTER', suite + ':', dir

        # if title contains newlines we just use the first line here
        title = title.split('\n')[0]
        self.items[suite] = dir, title

    def get( self, reg ):
        suite = self.unalias(reg)
        try:
            dir, title = self.items[suite]
        except KeyError:
            raise SuiteNotRegisteredError, "Suite not registered: " + suite
        return dir, title

    def getrc( self, reg ):
        dir, junk = self.get( reg )
        return os.path.join( dir, 'suite.rc' )

    def get_list( self, regfilter=None ):
        # Return a list of all registered suites, or a filtered list.
        # The list can be empty if no suites are registered, or if 
        # the filter rejects all registered suites.
        res = []
        for suite in self.items:
            if regfilter:
                try:
                    if not re.search(regfilter, suite):
                        continue
                except:
                    raise InvalidFilterError, regfilter
            dir, title = self.items[suite]
            res.append( [suite, dir, title] )
        return res

    def unregister( self, exp, regfilter=False ):
        if not regfilter:
            # Plain suite or group given; must match from beginning.
            exp = '^' + exp + r'\b'
        dirs = []
        for key in self.items.keys():
            if re.search( exp, key ):
                #if self.verbose:
                dir, junk = self.items[key]
                print 'UNREGISTER', key + ':', dir 
                if dir not in dirs:
                    # (there could be multiple registrations of the same
                    # suite defintion).
                    dirs.append(dir)
                del self.items[key]
        # check for aliases that now need to be unregistered
        for key in self.items.keys():
            dir, junk = self.items[key]
            if dir.startswith('->'):
                if re.search( exp, dir[2:] ):
                    #if self.verbose:
                    print 'UNREGISTER (invalidated alias)', key + ':', dir 
                    del self.items[key]
        return dirs

    def reregister( self, srce, targ ):
        found = False
        for key in self.items.keys():
            if key.startswith(srce):
                dir, title = self.items[key]
                newkey = re.sub( '^'+srce, targ, key )
                #if self.verbose:
                print 'REREGISTER', key, 'to', newkey
                del self.items[key]
                self.items[newkey] = dir, title
                found = True
        if not found:
            raise SuiteOrGroupNotFoundError, srce

    def alias( self, suite, alias ):
        pseudodir = '->' + suite
        self.register( alias, pseudodir )

    def unalias( self, alias ):
        try:
            dir, title = self.items[alias]
        except KeyError:
            raise SuiteNotFoundError, alias
        if dir.startswith('->'):
            target = self.unalias( dir[2:] )
        else:
            target = alias
        return target
         
    def get_invalid( self ):
        invalid = []
        for reg in self.items:
            suite = self.unalias(reg)
            dir, junk = self.items[suite]
            rcfile = os.path.join( dir, 'suite.rc' )
            if not os.path.isfile( rcfile ): 
                invalid.append( reg )
        return invalid

    def get_suite_title( self, suite, path=None ):
        if path:
            suiterc = os.path.join( path, 'suite.rc' )
        else:
            suite = self.unalias(suite)
            suiterc = self.getrc( suite )
        try:
            title = config( suite, suiterc ).get_title()
        except SuiteConfigError:
            title = "SUITE PARSING ERROR"
        return title

    def refresh_suite_title( self, suite ):
        dir, title = self.items[suite]
        new_title = self.get_suite_title( suite )
        if title == new_title:
            #if self.verbose:
            print 'unchanged:', suite#, '->', title
            changed = False
        else:
            print 'RETITLED:', suite #, '->', new_title
            changed = True
            self.items[suite] = dir, new_title
        return changed

    def get_rcfiles ( self, suite ):
        suite = self.unalias(suite)
        # return a list of all include-files used by this suite
        # TO DO: THIS NEEDS TO BE MADE RECURSIVE
        # (only used by cylc_xdot to check if graph has changed).
        rcfiles = []
        try:
            dir, junk = self.items[suite]
        except KeyError:
            raise SuiteNotFoundError, suite
        suiterc = os.path.join( dir, 'suite.rc' )
        rcfiles.append( suiterc )
        for line in open( suiterc, 'rb' ):
            m = re.match( '^\s*%include\s+([\/\w\-\.]+)', line )
            if m:
                rcfiles.append(os.path.join( dir, m.groups()[0]))
        return rcfiles

class localdb( regdb ):
    """
    Local (user-specific) suite registration database.
    """
    dir = local_regdb_dir
    def __init__( self, file=None, verbose=False):
        if file:
            # use for testing
            dir = os.path.dirname( file )
        else:
            # file in which to store suite registrations
            dir = self.__class__.dir
            file = os.path.join( dir, 'db' )
        regdb.__init__(self, dir, file, verbose)

class centraldb( regdb ):
    """
    Central registration database for sharing suites between users.
    """
    dir = central_regdb_dir
    def __init__( self, file=None, verbose=False ):
        if file:
            # use for testing
            dir = os.path.dirname( file )
        else:
            # file in which to store suite registrations
            dir = self.__class__.dir
            file = os.path.join( dir, 'db' )
        regdb.__init__(self, dir, file, verbose )

    def register( self, suite, dir, owner=None ):
        if owner:
            user = owner
        else:
            user = self.user
        regdb.register( self, user + delimiter + suite, dir )

    def reregister( self, srce, targ ):
        sreglist = srce.split(delimiter)
        treglist = targ.split(delimiter)
        if sreglist[0] != self.user:
            raise OwnerError, 'ERROR: You are not the owner of ' + srce
        if treglist[0] != self.user:
            raise OwnerError, 'ERROR: You cannot change your own username'
        regdb.reregister( self, srce, targ )

