# Copyright 2014 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Configuration storage and config-related utilities.

This borrows a lot of ideas from configparser, but also has some things that
are fundamentally different. This is why nothing inherts from configparser, but
we borrow some methods and classes from there where it makes sense.
"""

import os
import os.path
import logging
import textwrap
import configparser
from configparser import ConfigParser, ExtendedInterpolation
from collections.abc import MutableMapping

from PyQt5.QtCore import pyqtSignal, QObject

#from qutebrowser.utils.misc import read_file
import qutebrowser.config.configdata as configdata
import qutebrowser.commands.utils as cmdutils
import qutebrowser.utils.message as message
from qutebrowser.config.conftypes import ValidationError

config = None
state = None


class NoSectionError(configparser.NoSectionError):

    """Exception raised when a section was not found."""

    pass


class NoOptionError(configparser.NoOptionError):

    """Exception raised when an option was not found."""

    pass


def init(configdir):
    """Initialize the global objects based on the config in configdir.

    Args:
        configdir: The directory where the configs are stored in.
    """
    global config, state
    logging.debug("Config init, configdir {}".format(configdir))
    #config = Config(configdir, 'qutebrowser.conf',
    #                read_file('qutebrowser.conf'))
    config = Config(configdir, 'qutebrowser.conf')
    state = ReadWriteConfigParser(configdir, 'state')


class Config(QObject):

    """Configuration manager for qutebrowser.

    Attributes:
        config: The configuration data as an OrderedDict.
        _configparser: A ReadConfigParser instance to load the config.
        _wrapper_args: A dict with the default kwargs for the config wrappers.
        _configdir: The dictionary to read the config from and save it in.
        _configfile: The config file path.
        _interpolation: An configparser.Interpolation object
        _proxies: configparser.SectionProxy objects for sections.

    Signals:
        changed: Gets emitted when the config has changed.
                 Args: the changed section and option.
        style_changed: When style caches need to be invalidated.
                 Args: the changed section and option.
    """

    changed = pyqtSignal(str, str)
    style_changed = pyqtSignal(str, str)

    def __init__(self, configdir, fname, parent=None):
        super().__init__(parent)
        self.config = configdata.configdata()
        self._configparser = ReadConfigParser(configdir, fname)
        self._configfile = os.path.join(configdir, fname)
        self._wrapper_args = {
            'width': 72,
            'replace_whitespace': False,
            'break_long_words': False,
            'break_on_hyphens': False,
        }
        self._configdir = configdir
        self._interpolation = ExtendedInterpolation()
        self._proxies = {}
        for secname, section in self.config.items():
            self._proxies[secname] = SectionProxy(self, secname)
            try:
                section.from_cp(self._configparser[secname])
            except KeyError:
                pass

    def __getitem__(self, key):
        """Get a section from the config."""
        return self._proxies[key]

    def __str__(self):
        """Get the whole config as a string."""
        lines = configdata.FIRST_COMMENT.strip('\n').splitlines()
        for secname, section in self.config.items():
            lines.append('\n[{}]'.format(secname))
            lines += self._str_section_desc(secname)
            lines += self._str_option_desc(secname, section)
            lines += self._str_items(section)
        return '\n'.join(lines) + '\n'

    def _str_section_desc(self, secname):
        """Get the section description string for secname."""
        wrapper = textwrap.TextWrapper(initial_indent='# ',
                                       subsequent_indent='# ',
                                       **self._wrapper_args)
        lines = []
        seclines = configdata.SECTION_DESC[secname].splitlines()
        for secline in seclines:
            if 'http://' in secline:
                lines.append('# ' + secline)
            else:
                lines += wrapper.wrap(secline)
        return lines

    def _str_option_desc(self, secname, section):
        """Get the option description strings for section/secname."""
        wrapper = textwrap.TextWrapper(initial_indent='#' + ' ' * 5,
                                       subsequent_indent='#' + ' ' * 5,
                                       **self._wrapper_args)
        lines = []
        if not getattr(section, 'descriptions', None):
            return lines
        for optname, option in section.items():
            lines.append('#')
            if option.typ.typestr is None:
                typestr = ''
            else:
                typestr = ' ({})'.format(option.typ.typestr)
            lines.append('# {}{}:'.format(optname, typestr))
            try:
                desc = self.config[secname].descriptions[optname]
            except KeyError:
                continue
            for descline in desc.splitlines():
                lines += wrapper.wrap(descline)
            valid_values = option.typ.valid_values
            if valid_values is not None and valid_values.show:
                if valid_values.descriptions:
                    for val in valid_values:
                        desc = valid_values.descriptions[val]
                        lines += wrapper.wrap('    {}: {}'.format(val, desc))
                else:
                    lines += wrapper.wrap('Valid values: {}'.format(', '.join(
                        valid_values)))
            lines += wrapper.wrap('Default: {}'.format(
                option.values['default']))
        return lines

    def _str_items(self, section):
        """Get the option items as string for section."""
        lines = []
        for optname, option in section.items():
            keyval = '{} = {}'.format(optname, option.get_first_value(
                startlayer='conf'))
            lines.append(keyval)
        return lines

    def has_option(self, section, option):
        """Check if option exists in section.

        Arguments:
            section: The section name.
            option: The option name

        Return:
            True if the option and section exist, False otherwise.
        """
        if section not in self.config:
            return False
        return option in self.config[section]

    def remove_option(self, section, option):
        """Remove an option.

        Arguments:
            section: The section where to remove an option.
            option: The option name to remove.

        Return:
            True if the option existed, False otherwise.
        """
        try:
            sectdict = self.config[section]
        except KeyError:
            raise NoSectionError(section)
        option = self.optionxform(option)
        existed = option in sectdict
        if existed:
            del sectdict[option]
        return existed

    @cmdutils.register(name='get', instance='config',
                       completion=['section', 'option'])
    def get_wrapper(self, section, option):
        """Get the value from a section/option.

        Wrapper for the get-command to output the value in the status bar

        Arguments:
            section: Section to get the value from
            option: The option to get.

        Return:
            The value of the option.
        """
        val = self.get(section, option)
        message.info("{} {} = {}".format(section, option, val))

    def get(self, section, option, raw=False):
        """Get the value from a section/option.

        Arguments:
            section: The section to get the option from.
            option: The option name
            raw: Whether to get the uninterpolated, untransformed value.
        """
        logging.debug("getting {} -> {}".format(section, option))
        try:
            sect = self.config[section]
        except KeyError:
            raise NoSectionError(section)
        try:
            val = sect[option]
        except KeyError:
            raise NoOptionError(option, section)
        if raw:
            return val.value
        mapping = {key: val.value for key, val in sect.values.items()}
        newval = self._interpolation.before_get(self, section, option,
                                                val.value, mapping)
        logging.debug("interpolated val: {}".format(newval))
        newval = val.typ.transform(newval)
        return newval

    @cmdutils.register(name='set', instance='config',
                       completion=['section', 'option'])
    def set_wrapper(self, section, option, value):
        """Set an option.

        Wrapper for self.set() to output exceptions in the status bar.

        Arguments:
            *args: Get passed to self.set().
        """
        # FIXME completion for values
        try:
            self.set(section, option, value)
        except (NoOptionError, NoSectionError, ValidationError) as e:
            message.error("set: {} - {}".format(e.__class__.__name__, e))

    def set(self, section, option, value):
        """Set an option.

        Args:
            section: The name of the section to change.
            option: The name of the option to change.
            value: The new value.

        Raise:
            NoSectionError: If the specified section doesn't exist.
            NoOptionError: If the specified option doesn't exist.

        Emit:
            changed: If the config was changed.
            style_changed: When style caches need to be invalidated.
        """
        if value:
            value = self._interpolation.before_set(self, section, option,
                                                   value)
        try:
            sectdict = self.config[section]
        except KeyError:
            raise NoSectionError(section)
        try:
            sectdict[self.optionxform(option)] = value
        except KeyError:
            raise NoOptionError(option, section)
        else:
            if section in ['colors', 'fonts']:
                self.style_changed.emit(section, option)
            self.changed.emit(section, option)

    def save(self):
        """Save the config file."""
        if not os.path.exists(self._configdir):
            os.makedirs(self._configdir, 0o755)
        logging.debug("Saving config to {}".format(self._configfile))
        with open(self._configfile, 'w') as f:
            f.write(str(self))

    def dump_userconfig(self):
        """Get the part of the config which was changed by the user.

        Return:
            The changed config part as string.
        """
        # FIXME adopt this for layering
        lines = []
        for secname, section in self.config.items():
            changed_opts = []
            for optname, option in section.items():
                if (option.values.temp is not None and
                        option.values.temp != option.default or
                    option.values.conf is not None and
                        option.values.conf != option.default):
                    keyval = '{} = {}'.format(optname, option)  # FIXME layer?
                    changed_opts.append(keyval)
            if changed_opts:
                lines.append('[{}]'.format(secname))
                lines += changed_opts
        return '\n'.join(lines)

    def optionxform(self, val):
        """Implemented to be compatible with ConfigParser interpolation."""
        return val


class ReadConfigParser(ConfigParser):

    """Our own ConfigParser subclass to read the main config.

    Attributes:
        _configdir: The directory to read the config from.
        _configfile: The config file path.
    """

    def __init__(self, configdir, fname):
        """Config constructor.

        Args:
            configdir: Directory to read the config from.
            fname: Filename of the config file.
        """
        super().__init__(interpolation=None)
        self.optionxform = lambda opt: opt  # be case-insensitive
        self._configdir = configdir
        self._configfile = os.path.join(self._configdir, fname)
        if not os.path.isfile(self._configfile):
            return
        logging.debug("Reading config from {}".format(self._configfile))
        self.read(self._configfile)


class ReadWriteConfigParser(ReadConfigParser):

    """ConfigParser subclass used for auxillary config files."""

    def save(self):
        """Save the config file."""
        if not os.path.exists(self._configdir):
            os.makedirs(self._configdir, 0o755)
        logging.debug("Saving config to {}".format(self._configfile))
        with open(self._configfile, 'w') as f:
            self.write(f)


class SectionProxy(MutableMapping):

    """A proxy for a single section from a config.

    Attributes:
        _conf: The Config object.
        _name: The section name.
    """

    # pylint: disable=redefined-builtin

    def __init__(self, conf, name):
        """Create a view on a section.

        Arguments:
            conf: The Config object.
            name: The section name.
        """
        self._conf = conf
        self._name = name

    def __repr__(self):
        return '<Section: {}>'.format(self._name)

    def __getitem__(self, key):
        if not self._conf.has_option(self._name, key):
            raise KeyError(key)
        return self._conf.get(self._name, key)

    def __setitem__(self, key, value):
        return self._conf.set(self._name, key, value)

    def __delitem__(self, key):
        if not (self._conf.has_option(self._name, key) and
                self._conf.remove_option(self._name, key)):
            raise KeyError(key)

    def __contains__(self, key):
        return self._conf.has_option(self._name, key)

    def __len__(self):
        return len(self._options())

    def __iter__(self):
        return self._options().__iter__()

    def _options(self):
        """Get the option keys from this section."""
        return self._conf.config[self._name].values.keys()

    def get(self, option, *, raw=False):
        """Get a value from this section.

        We deliberately don't support the default argument here, but have a raw
        argument instead.

        Arguments:
            option: The option name to get.
            raw: Whether to get a raw value or not.
        """
        # pylint: disable=arguments-differ
        return self._conf.get(self._name, option, raw=raw)

    @property
    def conf(self):
        """The conf object of the proxy is read-only."""
        return self._conf

    @property
    def name(self):
        """The name of the section on a proxy is read-only."""
        return self._name
