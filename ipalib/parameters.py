# Authors:
#   Jason Gerard DeRose <jderose@redhat.com>
#
# Copyright (C) 2008  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Parameter system for command plugins.

A `Param` instance can be used to describe an argument or option that a command
takes, or an attribute that a command returns.  The `Param` base class is not
used directly, but there are many subclasses for specific Python data types
(like `Str` or `Int`) and specific properties (like `Password`).

To create a `Param` instance, you must always provide the parameter *name*,
which should be the LDAP attribute name if the parameter describes the attribute
of an LDAP entry.  For example, we could create an `Str` instance describing the user's last-name attribute like this:

>>> from ipalib import Str
>>> sn = Str('sn')
>>> sn.name
'sn'

When creating a `Param`, there are also a number of optional kwargs which
which can provide additional meta-data and functionality.  For example, every
parameter has a *cli_name*, the name used on the command-line-interface.  By
default the *cli_name* is the same as the *name*:

>>> sn.cli_name
'sn'

But often the LDAP attribute name isn't user friendly for the command-line, so
you can override this with the *cli_name* kwarg:

>>> sn = Str('sn', cli_name='last')
>>> sn.name
'sn'
>>> sn.cli_name
'last'

Note that the RPC interfaces (and the internal processing pipeline) always use
the parameter *name*, regardless of what the *cli_name* might be.

A `Param` also has two translatable kwargs: *label* and *doc*.  These must both
be `Gettext` instances.  They both default to a place-holder `FixMe` instance,
a subclass of `Gettext` used to mark a missing translatable string:

>>> sn.label
FixMe('sn')
>>> sn.doc
FixMe('sn')

The *label* is a short phrase describing the parameter.  It's used on the CLI
when interactively prompting for values, and as a label for form inputs in the
web-UI.  The *label* should start with an initial capital.  For example:

>>> from ipalib import _
>>> sn = Str('sn',
...     cli_name='last',
...     label=_('Last name'),
... )
>>> sn.label
Gettext('Last name', domain='ipa', localedir=None)

The *doc* is a longer description of the parameter.  It's used on the CLI when
displaying the help information for a command, and as extra instruction for a
form input on the web-UI.  By default the *doc* is the same as the *label*:

>>> sn.doc
Gettext('Last name', domain='ipa', localedir=None)

But you can override this with the *doc* kwarg.  Like the *label*, the *doc*
should also start with an initial capital and should not end with any
punctuation.  For example:

>>> sn = Str('sn',
...     cli_name='last',
...     label=_('Last name'),
...     doc=_("The user's last name"),
... )
>>> sn.doc
Gettext("The user's last name", domain='ipa', localedir=None)

Demonstration aside, you should always provide at least the *label* so the
various UIs are translatable.  Only provide the *doc* if the parameter needs
a more detailed description for clarity.
"""

import re
from types import NoneType
from util import make_repr
from text import _ as ugettext
from plugable import ReadOnly, lock, check_name
from errors import ConversionError, RequirementError, ValidationError
from errors import PasswordMismatch
from constants import NULLS, TYPE_ERROR, CALLABLE_ERROR
from text import Gettext, FixMe
import csv
from xmlrpclib import MAXINT


class DefaultFrom(ReadOnly):
    """
    Derive a default value from other supplied values.

    For example, say you wanted to create a default for the user's login from
    the user's first and last names. It could be implemented like this:

    >>> login = DefaultFrom(lambda first, last: first[0] + last)
    >>> login(first='John', last='Doe')
    'JDoe'

    If you do not explicitly provide keys when you create a `DefaultFrom`
    instance, the keys are implicitly derived from your callback by
    inspecting ``callback.func_code.co_varnames``. The keys are available
    through the ``DefaultFrom.keys`` instance attribute, like this:

    >>> login.keys
    ('first', 'last')

    The callback is available through the ``DefaultFrom.callback`` instance
    attribute, like this:

    >>> login.callback  # doctest:+ELLIPSIS
    <function <lambda> at 0x...>
    >>> login.callback.func_code.co_varnames  # The keys
    ('first', 'last')

    The keys can be explicitly provided as optional positional arguments after
    the callback. For example, this is equivalent to the ``login`` instance
    above:

    >>> login2 = DefaultFrom(lambda a, b: a[0] + b, 'first', 'last')
    >>> login2.keys
    ('first', 'last')
    >>> login2.callback.func_code.co_varnames  # Not the keys
    ('a', 'b')
    >>> login2(first='John', last='Doe')
    'JDoe'

    If any keys are missing when calling your `DefaultFrom` instance, your
    callback is not called and ``None`` is returned.  For example:

    >>> login(first='John', lastname='Doe') is None
    True
    >>> login() is None
    True

    Any additional keys are simply ignored, like this:

    >>> login(last='Doe', first='John', middle='Whatever')
    'JDoe'

    As above, because `DefaultFrom.__call__` takes only pure keyword
    arguments, they can be supplied in any order.

    Of course, the callback need not be a ``lambda`` expression. This third
    example is equivalent to both the ``login`` and ``login2`` instances
    above:

    >>> def get_login(first, last):
    ...     return first[0] + last
    ...
    >>> login3 = DefaultFrom(get_login)
    >>> login3.keys
    ('first', 'last')
    >>> login3.callback.func_code.co_varnames
    ('first', 'last')
    >>> login3(first='John', last='Doe')
    'JDoe'
    """

    def __init__(self, callback, *keys):
        """
        :param callback: The callable to call when all keys are present.
        :param keys: Optional keys used for source values.
        """
        if not callable(callback):
            raise TypeError(
                CALLABLE_ERROR % ('callback', callback, type(callback))
            )
        self.callback = callback
        if len(keys) == 0:
            fc = callback.func_code
            self.keys = fc.co_varnames[:fc.co_argcount]
        else:
            self.keys = keys
        for key in self.keys:
            if type(key) is not str:
                raise TypeError(
                    TYPE_ERROR % ('keys', str, key, type(key))
                )
        lock(self)

    def __repr__(self):
        args = (self.callback.__name__,) + tuple(repr(k) for k in self.keys)
        return '%s(%s)' % (
            self.__class__.__name__,
            ', '.join(args)
        )

    def __call__(self, **kw):
        """
        Call the callback if all keys are present.

        If all keys are present, the callback is called and its return value is
        returned.  If any keys are missing, ``None`` is returned.

        :param kw: The keyword arguments.
        """
        vals = tuple(kw.get(k, None) for k in self.keys)
        if None in vals:
            return
        try:
            return self.callback(*vals)
        except StandardError:
            pass


def parse_param_spec(spec):
    """
    Parse shorthand ``spec`` into to ``(name, kw)``.

    The ``spec`` string determines the parameter name, whether the parameter is
    required, and whether the parameter is multivalue according the following
    syntax:

    ======  =====  ========  ==========
    Spec    Name   Required  Multivalue
    ======  =====  ========  ==========
    'var'   'var'  True      False
    'var?'  'var'  False     False
    'var*'  'var'  False     True
    'var+'  'var'  True      True
    ======  =====  ========  ==========

    For example,

    >>> parse_param_spec('login')
    ('login', {'required': True, 'multivalue': False})
    >>> parse_param_spec('gecos?')
    ('gecos', {'required': False, 'multivalue': False})
    >>> parse_param_spec('telephone_numbers*')
    ('telephone_numbers', {'required': False, 'multivalue': True})
    >>> parse_param_spec('group+')
    ('group', {'required': True, 'multivalue': True})

    :param spec: A spec string.
    """
    if type(spec) is not str:
        raise TypeError(
            TYPE_ERROR % ('spec', str, spec, type(spec))
        )
    _map = {
        '?': dict(required=False, multivalue=False),
        '*': dict(required=False, multivalue=True),
        '+': dict(required=True, multivalue=True),
    }
    end = spec[-1]
    if end in _map:
        return (spec[:-1], _map[end])
    return (spec, dict(required=True, multivalue=False))


__messages = set()

def _(message):
    __messages.add(message)
    return message


class Param(ReadOnly):
    """
    Base class for all parameters.
    """

    # This is a dummy type so that most of the functionality of Param can be
    # unit tested directly without always creating a subclass; however, a real
    # (direct) subclass must *always* override this class attribute:
    type = NoneType  # Ouch, this wont be very useful in the real world!

    # Subclasses should override this with something more specific:
    type_error = _('incorrect type')

    # _convert_scalar operates only on scalar values
    scalar_error = _('Only one value is allowed')

    kwargs = (
        ('cli_name', str, None),
        ('cli_short_name', str, None),
        ('label', (str, Gettext), None),
        ('doc', (basestring, Gettext), None),
        ('required', bool, True),
        ('multivalue', bool, False),
        ('primary_key', bool, False),
        ('normalizer', callable, None),
        ('default_from', DefaultFrom, None),
        ('create_default', callable, None),
        ('autofill', bool, False),
        ('query', bool, False),
        ('attribute', bool, False),
        ('include', frozenset, None),
        ('exclude', frozenset, None),
        ('flags', frozenset, frozenset()),
        ('hint', (str, Gettext), None),
        ('alwaysask', bool, False),

        # The 'default' kwarg gets appended in Param.__init__():
        # ('default', self.type, None),
    )

    def __init__(self, name, *rules, **kw):
        # We keep these values to use in __repr__():
        self.param_spec = name
        self.__kw = dict(kw)

        if isinstance(self, Password):
            self.password = True
        else:
            self.password = False

        # Merge in kw from parse_param_spec():
        (name, kw_from_spec) = parse_param_spec(name)
        if not 'required' in kw:
            kw['required'] = kw_from_spec['required']
        if not 'multivalue' in kw:
            kw['multivalue'] = kw_from_spec['multivalue']
        self.name = check_name(name)
        self.nice = '%s(%r)' % (self.__class__.__name__, self.param_spec)

        # Add 'default' to self.kwargs and makes sure no unknown kw were given:
        assert type(self.type) is type
        if kw.get('multivalue', True):
            self.kwargs += (('default', tuple, None),)
        else:
            self.kwargs += (('default', self.type, None),)
        if not set(t[0] for t in self.kwargs).issuperset(self.__kw):
            extra = set(kw) - set(t[0] for t in self.kwargs)
            raise TypeError(
                '%s: takes no such kwargs: %s' % (self.nice,
                    ', '.join(repr(k) for k in sorted(extra))
                )
            )

        # Merge in default for 'cli_name', label, doc if not given:
        if kw.get('cli_name') is None:
            kw['cli_name'] = self.name

        if kw.get('label') is None:
            kw['label'] = FixMe(self.name)

        if kw.get('doc') is None:
            kw['doc'] = kw['label']

        # Wrap 'default_from' in a DefaultFrom if not already:
        df = kw.get('default_from', None)
        if callable(df) and not isinstance(df, DefaultFrom):
            kw['default_from'] = DefaultFrom(df)

        # We keep this copy with merged values also to use when cloning:
        self.__clonekw = kw

        # Perform type validation on kw, add in class rules:
        class_rules = []
        for (key, kind, default) in self.kwargs:
            value = kw.get(key, default)
            if value is not None:
                if kind is frozenset:
                    if type(value) in (list, tuple):
                        value = frozenset(value)
                    elif type(value) is str:
                        value = frozenset([value])
                if (
                    type(kind) is type and not isinstance(value, kind)
                    or
                    type(kind) is tuple and not isinstance(value, kind)
                ):
                    raise TypeError(
                        TYPE_ERROR % (key, kind, value, type(value))
                    )
                elif kind is callable and not callable(value):
                    raise TypeError(
                        CALLABLE_ERROR % (key, value, type(value))
                    )
            if hasattr(self, key):
                raise ValueError('kwarg %r conflicts with attribute on %s' % (
                    key, self.__class__.__name__)
                )
            setattr(self, key, value)
            rule_name = '_rule_%s' % key
            if value is not None and hasattr(self, rule_name):
                class_rules.append(getattr(self, rule_name))
        check_name(self.cli_name)

        # Check that only default_from or create_default was provided:
        assert not hasattr(self, '_get_default'), self.nice
        if callable(self.default_from):
            if callable(self.create_default):
                raise ValueError(
                    '%s: cannot have both %r and %r' % (
                        self.nice, 'default_from', 'create_default')
                )
            self._get_default = self.default_from
        elif callable(self.create_default):
            self._get_default = self.create_default
        else:
            self._get_default = None

        # Check that only 'include' or 'exclude' was provided:
        if None not in (self.include, self.exclude):
            raise ValueError(
                '%s: cannot have both %s=%r and %s=%r' % (
                    self.nice,
                    'include', self.include,
                    'exclude', self.exclude,
                )
            )

        # Check that all the rules are callable
        self.class_rules = tuple(class_rules)
        self.rules = rules
        self.all_rules = self.class_rules + self.rules
        for rule in self.all_rules:
            if not callable(rule):
                raise TypeError(
                    '%s: rules must be callable; got %r' % (self.nice, rule)
                )

        # Check that cli_short_name is only 1 character long:
        if not (self.cli_short_name is None or len(self.cli_short_name) == 1):
            raise ValueError(
                '%s: cli_short_name can only be a single character: %s' % (
                    self.nice, self.cli_short_name)
            )

        # And we're done.
        lock(self)

    def __repr__(self):
        """
        Return an expresion that could construct this `Param` instance.
        """
        return '%s(%s)' % (
            self.__class__.__name__,
            ', '.join(self.__repr_iter())
        )

    def __repr_iter(self):
        yield repr(self.param_spec)
        for rule in self.rules:
            yield rule.__name__
        for key in sorted(self.__kw):
            value = self.__kw[key]
            if callable(value) and hasattr(value, '__name__'):
                value = value.__name__
            else:
                value = repr(value)
            yield '%s=%s' % (key, value)

    def __call__(self, value, **kw):
        """
        One stop shopping.
        """
        if value in NULLS:
            value = self.get_default(**kw)
        else:
            value = self.convert(self.normalize(value))
        if hasattr(self, 'env'):
            self.validate(value, self.env.context)  #pylint: disable=E1101
        else:
            self.validate(value)
        return value

    def kw(self):
        """
        Iterate through ``(key,value)`` for all kwargs passed to constructor.
        """
        for key in sorted(self.__kw):
            value = self.__kw[key]
            if callable(value) and hasattr(value, '__name__'):
                value = value.__name__
            yield (key, value)

    def use_in_context(self, env):
        """
        Return ``True`` if this parameter should be used in ``env.context``.

        If a parameter is created with niether the ``include`` nor the
        ``exclude`` kwarg, this method will always return ``True``.  For
        example:

        >>> from ipalib.config import Env
        >>> param = Param('my_param')
        >>> param.use_in_context(Env(context='foo'))
        True
        >>> param.use_in_context(Env(context='bar'))
        True

        If a parameter is created with an ``include`` kwarg, this method will
        only return ``True`` if ``env.context`` is in ``include``.  For example:

        >>> param = Param('my_param', include=['foo', 'whatever'])
        >>> param.include
        frozenset(['foo', 'whatever'])
        >>> param.use_in_context(Env(context='foo'))
        True
        >>> param.use_in_context(Env(context='bar'))
        False

        If a paremeter is created with an ``exclude`` kwarg, this method will
        only return ``True`` if ``env.context`` is not in ``exclude``.  For
        example:

        >>> param = Param('my_param', exclude=['foo', 'whatever'])
        >>> param.exclude
        frozenset(['foo', 'whatever'])
        >>> param.use_in_context(Env(context='foo'))
        False
        >>> param.use_in_context(Env(context='bar'))
        True

        Note that the ``include`` and ``exclude`` kwargs are mutually exclusive
        and that at most one can be suppelied to `Param.__init__()`.  For
        example:

        >>> param = Param('nope', include=['foo'], exclude=['bar'])
        Traceback (most recent call last):
          ...
        ValueError: Param('nope'): cannot have both include=frozenset(['foo']) and exclude=frozenset(['bar'])

        So that subclasses can add additional logic based on other environment
        variables, the entire `config.Env` instance is passed in rather than
        just the value of ``env.context``.
        """
        if self.include is not None:
            return (env.context in self.include)
        if self.exclude is not None:
            return (env.context not in self.exclude)
        return True

    def safe_value(self, value):
        """
        Return a value safe for logging.

        This is used so that passwords don't get logged.  If this is a
        `Password` instance and ``value`` is not ``None``, a constant
        ``u'********'`` is returned.  For example:

        >>> p = Password('my_password')
        >>> p.safe_value(u'This is my password')
        u'********'
        >>> p.safe_value(None) is None
        True

        If this is not a `Password` instance, ``value`` is returned unchanged.
        For example:

        >>> s = Str('my_str')
        >>> s.safe_value(u'Some arbitrary value')
        u'Some arbitrary value'
        """
        if self.password and value is not None:
            return u'********'
        return value

    def clone(self, **overrides):
        """
        Return a new `Param` instance similar to this one.
        """
        return self.clone_rename(self.name, **overrides)

    def clone_rename(self, name, **overrides):
        """
        Return a new `Param` instance similar to this one, but named differently
        """
        return self.clone_retype(name, self.__class__, **overrides)

    def clone_retype(self, name, klass, **overrides):
        """
        Return a new `Param` instance similar to this one, but of a different type
        """
        kw = dict(self.__clonekw)
        kw.update(overrides)
        return klass(name, *self.rules, **kw)

    def normalize(self, value):
        """
        Normalize ``value`` using normalizer callback.

        For example:

        >>> param = Param('telephone',
        ...     normalizer=lambda value: value.replace('.', '-')
        ... )
        >>> param.normalize(u'800.123.4567')
        u'800-123-4567'

        If this `Param` instance was created with a normalizer callback and
        ``value`` is a unicode instance, the normalizer callback is called and
        *its* return value is returned.

        On the other hand, if this `Param` instance was *not* created with a
        normalizer callback, if ``value`` is *not* a unicode instance, or if an
        exception is caught when calling the normalizer callback, ``value`` is
        returned unchanged.

        :param value: A proposed value for this parameter.
        """
        if self.normalizer is None:
            return value
        if self.multivalue:
            if type(value) in (tuple, list):
                return tuple(
                    self._normalize_scalar(v) for v in value
                )
            return (self._normalize_scalar(value),)  # Return a tuple
        return self._normalize_scalar(value)

    def _normalize_scalar(self, value):
        """
        Normalize a scalar value.

        This method is called once for each value in a multivalue.
        """
        if type(value) is not unicode:
            return value
        try:
            return self.normalizer(value)
        except StandardError:
            return value

    def convert(self, value):
        """
        Convert ``value`` to the Python type required by this parameter.

        For example:

        >>> scalar = Str('my_scalar')
        >>> scalar.type
        <type 'unicode'>
        >>> scalar.convert(43.2)
        u'43.2'

        (Note that `Str` is a subclass of `Param`.)

        All values in `constants.NULLS` will be converted to ``None``.  For
        example:

        >>> scalar.convert(u'') is None  # An empty string
        True
        >>> scalar.convert([]) is None  # An empty list
        True

        Likewise, values in `constants.NULLS` will be filtered out of a
        multivalue parameter.  For example:

        >>> multi = Str('my_multi', multivalue=True)
        >>> multi.convert([1.5, '', 17, None, u'Hello'])
        (u'1.5', u'17', u'Hello')
        >>> multi.convert([None, u'']) is None  # Filters to an empty list
        True

        Lastly, multivalue parameters will always return a ``tuple`` (assuming
        they don't return ``None`` as in the last example above).  For example:

        >>> multi.convert(42)  # Called with a scalar value
        (u'42',)
        >>> multi.convert([0, 1])  # Called with a list value
        (u'0', u'1')

        Note that how values are converted (and from what types they will be
        converted) completely depends upon how a subclass implements its
        `Param._convert_scalar()` method.  For example, see
        `Str._convert_scalar()`.

        :param value: A proposed value for this parameter.
        """
        if value in NULLS:
            return
        if self.multivalue:
            if type(value) not in (tuple, list):
                value = (value,)
            values = tuple(
                self._convert_scalar(v, i) for (i, v) in filter(
                    lambda iv: iv[1] not in NULLS, enumerate(value)
                )
            )
            if len(values) == 0:
                return
            return values
        return self._convert_scalar(value)

    def _convert_scalar(self, value, index=None):
        """
        Convert a single scalar value.
        """
        if type(value) is self.type:
            return value
        raise ConversionError(name=self.name, index=index,
            error=ugettext(self.type_error),
        )

    def validate(self, value, context=None):
        """
        Check validity of ``value``.

        :param value: A proposed value for this parameter.
        :param context: The context we are running in.
        """
        if value is None:
            if self.required:
                if context == 'cli':
                    raise RequirementError(name=self.cli_name)
                else:
                    raise RequirementError(name=self.name)
            return
        if self.query:
            return
        if self.multivalue:
            if type(value) is not tuple:
                raise TypeError(
                    TYPE_ERROR % ('value', tuple, value, type(value))
                )
            if len(value) < 1:
                raise ValueError('value: empty tuple must be converted to None')
            for (i, v) in enumerate(value):
                self._validate_scalar(v, i)
        else:
            self._validate_scalar(value)

    def _validate_scalar(self, value, index=None):
        if type(value) is not self.type:
            raise ValidationError(name=self.name,
                error='need a %r; got %r (a %r)' % (
                    self.type, value, type(value)
                )
            )
        if index is not None and type(index) is not int:
            raise TypeError(
                TYPE_ERROR % ('index', int, index, type(index))
            )
        for rule in self.all_rules:
            error = rule(ugettext, value)
            if error is not None:
                name = self.cli_name
                if not name:
                    name = self.name
                raise ValidationError(
                    name=name,
                    value=value,
                    index=index,
                    error=error,
                    rule=rule,
                )

    def get_default(self, **kw):
        """
        Return the static default or construct and return a dynamic default.

        (In these examples, we will use the `Str` and `Bytes` classes, which
        both subclass from `Param`.)

        The *default* static default is ``None``.  For example:

        >>> s = Str('my_str')
        >>> s.default is None
        True
        >>> s.get_default() is None
        True

        However, you can provide your own static default via the ``default``
        keyword argument when you create your `Param` instance.  For example:

        >>> s = Str('my_str', default=u'My Static Default')
        >>> s.default
        u'My Static Default'
        >>> s.get_default()
        u'My Static Default'

        If you need to generate a dynamic default from other supplied parameter
        values, provide a callback via the ``default_from`` keyword argument.
        This callback will be automatically wrapped in a `DefaultFrom` instance
        if it isn't one already (see the `DefaultFrom` class for all the gory
        details).  For example:

        >>> login = Str('login', default=u'my-static-login-default',
        ...     default_from=lambda first, last: (first[0] + last).lower(),
        ... )
        >>> isinstance(login.default_from, DefaultFrom)
        True
        >>> login.default_from.keys
        ('first', 'last')

        Then when all the keys needed by the `DefaultFrom` instance are present,
        the dynamic default is constructed and returned.  For example:

        >>> kw = dict(last=u'Doe', first=u'John')
        >>> login.get_default(**kw)
        u'jdoe'

        Or if any keys are missing, your *static* default is returned.
        For example:

        >>> kw = dict(first=u'John', department=u'Engineering')
        >>> login.get_default(**kw)
        u'my-static-login-default'

        The second, less common way to construct a dynamic default is to provide
        a callback via the ``create_default`` keyword argument.  Unlike a
        ``default_from`` callback, your ``create_default`` callback will not get
        wrapped in any dispatcher.  Instead, it will be called directly, which
        means your callback must accept arbitrary keyword arguments, although
        whether your callback utilises these values is up to your
        implementation.  For example:

        >>> def make_csr(**kw):
        ...     print '  make_csr(%r)' % (kw,)  # Note output below
        ...     return 'Certificate Signing Request'
        ...
        >>> csr = Bytes('csr', create_default=make_csr)

        Your ``create_default`` callback will be called with whatever keyword
        arguments are passed to `Param.get_default()`.  For example:

        >>> kw = dict(arbitrary='Keyword', arguments='Here')
        >>> csr.get_default(**kw)
          make_csr({'arguments': 'Here', 'arbitrary': 'Keyword'})
        'Certificate Signing Request'

        And your ``create_default`` callback is called even if
        `Param.get_default()` is called with *zero* keyword arguments.
        For example:

        >>> csr.get_default()
          make_csr({})
        'Certificate Signing Request'

        The ``create_default`` callback will most likely be used as a
        pre-execute hook to perform some special client-side operation.  For
        example, the ``csr`` parameter above might make a call to
        ``/usr/bin/openssl``.  However, often a ``create_default`` callback
        could also be implemented as a ``default_from`` callback.  When this is
        the case, a ``default_from`` callback should be used as they are more
        structured and therefore less error-prone.

        The ``default_from`` and ``create_default`` keyword arguments are
        mutually exclusive.  If you provide both, a ``ValueError`` will be
        raised.  For example:

        >>> homedir = Str('home',
        ...     default_from=lambda login: '/home/%s' % login,
        ...     create_default=lambda **kw: '/lets/use/this',
        ... )
        Traceback (most recent call last):
          ...
        ValueError: Str('home'): cannot have both 'default_from' and 'create_default'
        """
        if self._get_default is not None:
            default = self._get_default(**kw)
            if default is not None:
                try:
                    return self.convert(self.normalize(default))
                except StandardError:
                    pass
        return self.default

    def __json__(self):
        json_dict = {}
        for (a, k, d) in self.kwargs:
            if k in (callable, DefaultFrom):
                continue
            elif isinstance(getattr(self, a), frozenset):
                json_dict[a] = [k for k in getattr(self, a, [])]
            else:
                json_dict[a] = getattr(self, a, '')
        json_dict['class'] = self.__class__.__name__
        json_dict['name'] = self.name
        json_dict['type'] = self.type.__name__
        return json_dict


class Bool(Param):
    """
    A parameter for boolean values (stored in the ``bool`` type).
    """

    type = bool
    type_error = _('must be True or False')

    # FIXME: This my quick hack to get some UI stuff working, change these defaults
    #   --jderose 2009-08-28
    kwargs = Param.kwargs + (
        ('truths', frozenset, frozenset([1, u'1', u'true'])),
        ('falsehoods', frozenset, frozenset([0, u'0', u'false'])),
    )

    def _convert_scalar(self, value, index=None):
        """
        Convert a single scalar value.
        """
        if type(value) is self.type:
            return value
        if isinstance(value, basestring):
            value = value.lower()
        if value in self.truths:
            return True
        if value in self.falsehoods:
            return False
        if type(value) in (tuple, list):
            raise ConversionError(name=self.name, index=index,
            error=ugettext(self.scalar_error))
        raise ConversionError(name=self.name, index=index,
            error=ugettext(self.type_error),
        )


class Flag(Bool):
    """
    A boolean parameter that always gets filled in with a default value.

    This `Bool` subclass forces ``autofill=True`` in `Flag.__init__()`.  If no
    default is provided, it also fills in a default value of ``False``.
    Lastly, unlike the `Bool` class, the default must be either ``True`` or
    ``False`` and cannot be ``None``.

    For example:

    >>> flag = Flag('my_flag')
    >>> (flag.autofill, flag.default)
    (True, False)

    To have a default value of ``True``, create your `Flag` intance with
    ``default=True``.  For example:

    >>> flag = Flag('my_flag', default=True)
    >>> (flag.autofill, flag.default)
    (True, True)

    Also note that creating a `Flag` instance with ``autofill=False`` will have
    no effect.  For example:

    >>> flag = Flag('my_flag', autofill=False)
    >>> flag.autofill
    True
    """

    def __init__(self, name, *rules, **kw):
        kw['autofill'] = True
        if 'default' not in kw:
            kw['default'] = False
        if type(kw['default']) is not bool:
            default = kw['default']
            raise TypeError(
                TYPE_ERROR % ('default', bool, default, type(default))
            )
        super(Flag, self).__init__(name, *rules, **kw)


class Number(Param):
    """
    Base class for the `Int` and `Float` parameters.
    """

    def _convert_scalar(self, value, index=None):
        """
        Convert a single scalar value.
        """
        if type(value) is self.type:
            return value
        if type(value) in (unicode, int, float):
            try:
                return self.type(value)
            except ValueError:
                pass
        if type(value) in (tuple, list):
            raise ConversionError(name=self.name, index=index,
            error=ugettext(self.scalar_error))
        raise ConversionError(name=self.name, index=index,
            error=ugettext(self.type_error),
        )


class Int(Number):
    """
    A parameter for integer values (stored in the ``int`` type).
    """

    type = int
    type_error = _('must be an integer')

    kwargs = Param.kwargs + (
        ('minvalue', int, None),
        ('maxvalue', int, int(MAXINT)),
    )

    def __init__(self, name, *rules, **kw):
        #pylint: disable=E1003
        super(Number, self).__init__(name, *rules, **kw)

        if (self.minvalue > self.maxvalue) and (self.minvalue is not None and self.maxvalue is not None):
            raise ValueError(
                '%s: minvalue > maxvalue (minvalue=%r, maxvalue=%r)' % (
                    self.nice, self.minvalue, self.maxvalue)
            )

    def _convert_scalar(self, value, index=None):
        """
        Convert a single scalar value.
        """
        if type(value) in (int, long):
            return value
        if type(value) is unicode:
            # permit floating point strings
            if value.find(u'.') >= 0:
                try:
                    return int(float(value))
                except ValueError:
                    pass
            else:
                try:
                    # 2nd arg is radix base, 2nd arg only accepted for strings.
                    # Zero means determine radix base from prefix (e.g. 0x for hex)
                    return int(value, 0)
                except ValueError:
                    pass
        if type(value) is float:
            try:
                return int(value)
            except ValueError:
                pass
        raise ConversionError(name=self.name, index=index,
            error=ugettext(self.type_error),
        )

    def _rule_minvalue(self, _, value):
        """
        Check min constraint.
        """
        assert type(value) is int
        if value < self.minvalue:
            return _('must be at least %(minvalue)d') % dict(
                minvalue=self.minvalue,
            )

    def _rule_maxvalue(self, _, value):
        """
        Check max constraint.
        """
        assert type(value) is int
        if value > self.maxvalue:
            return _('can be at most %(maxvalue)d') % dict(
                maxvalue=self.maxvalue,
            )

class Float(Number):
    """
    A parameter for floating-point values (stored in the ``float`` type).
    """

    type = float
    type_error = _('must be a decimal number')

    kwargs = Param.kwargs + (
        ('minvalue', float, None),
        ('maxvalue', float, None),
    )

    def __init__(self, name, *rules, **kw):
        #pylint: disable=E1003
        super(Number, self).__init__(name, *rules, **kw)

        if (self.minvalue > self.maxvalue) and (self.minvalue is not None and self.maxvalue is not None):
            raise ValueError(
                '%s: minvalue > maxvalue (minvalue=%r, maxvalue=%r)' % (
                    self.nice, self.minvalue, self.maxvalue)
            )

    def _rule_minvalue(self, _, value):
        """
        Check min constraint.
        """
        assert type(value) is float
        if value < self.minvalue:
            return _('must be at least %(minvalue)f') % dict(
                minvalue=self.minvalue,
            )

    def _rule_maxvalue(self, _, value):
        """
        Check max constraint.
        """
        assert type(value) is float
        if value > self.maxvalue:
            return _('can be at most %(maxvalue)f') % dict(
                maxvalue=self.maxvalue,
            )


class Data(Param):
    """
    Base class for the `Bytes` and `Str` parameters.

    Previously `Str` was as subclass of `Bytes`.  Now the common functionality
    has been split into this base class so that ``isinstance(foo, Bytes)`` wont
    be ``True`` when ``foo`` is actually an `Str` instance (which is confusing).
    """

    kwargs = Param.kwargs + (
        ('minlength', int, None),
        ('maxlength', int, None),
        ('length', int, None),
        ('pattern', (basestring,), None),
        ('pattern_errmsg', (basestring,), None),
    )

    def __init__(self, name, *rules, **kw):
        super(Data, self).__init__(name, *rules, **kw)

        if not (
            self.length is None or
            (self.minlength is None and self.maxlength is None)
        ):
            raise ValueError(
                '%s: cannot mix length with minlength or maxlength' % self.nice
            )

        if self.minlength is not None and self.minlength < 1:
            raise ValueError(
                '%s: minlength must be >= 1; got %r' % (self.nice, self.minlength)
            )

        if self.maxlength is not None and self.maxlength < 1:
            raise ValueError(
                '%s: maxlength must be >= 1; got %r' % (self.nice, self.maxlength)
            )

        if None not in (self.minlength, self.maxlength):
            if self.minlength > self.maxlength:
                raise ValueError(
                    '%s: minlength > maxlength (minlength=%r, maxlength=%r)' % (
                        self.nice, self.minlength, self.maxlength)
                )
            elif self.minlength == self.maxlength:
                raise ValueError(
                    '%s: minlength == maxlength; use length=%d instead' % (
                        self.nice, self.minlength)
                )

    def _rule_pattern(self, _, value):
        """
        Check pattern (regex) contraint.
        """
        assert type(value) is self.type
        if self.re.match(value) is None:
            if self.re_errmsg:
                return self.re_errmsg % dict(pattern=self.pattern,)
            else:
                return _('must match pattern "%(pattern)s"') % dict(
                    pattern=self.pattern,
                )


class Bytes(Data):
    """
    A parameter for binary data (stored in the ``str`` type).

    This class is named *Bytes* instead of *Str* so it's aligned with the
    Python v3 ``(str, unicode) => (bytes, str)`` clean-up.  See:

        http://docs.python.org/3.0/whatsnew/3.0.html

    Also see the `Str` parameter.
    """

    type = str
    type_error = _('must be binary data')

    def __init__(self, name, *rules, **kw):
        if kw.get('pattern', None) is None:
            self.re = None
        else:
            self.re = re.compile(kw['pattern'])
        self.re_errmsg = kw.get('pattern_errmsg', None)
        super(Bytes, self).__init__(name, *rules, **kw)

    def _rule_minlength(self, _, value):
        """
        Check minlength constraint.
        """
        assert type(value) is str
        if len(value) < self.minlength:
            return _('must be at least %(minlength)d bytes') % dict(
                minlength=self.minlength,
            )

    def _rule_maxlength(self, _, value):
        """
        Check maxlength constraint.
        """
        assert type(value) is str
        if len(value) > self.maxlength:
            return _('can be at most %(maxlength)d bytes') % dict(
                maxlength=self.maxlength,
            )

    def _rule_length(self, _, value):
        """
        Check length constraint.
        """
        assert type(value) is str
        if len(value) != self.length:
            return _('must be exactly %(length)d bytes') % dict(
                length=self.length,
            )


class Str(Data):
    """
    A parameter for Unicode text (stored in the ``unicode`` type).

    This class is named *Str* instead of *Unicode* so it's aligned with the
    Python v3 ``(str, unicode) => (bytes, str)`` clean-up.  See:

        http://docs.python.org/3.0/whatsnew/3.0.html

    Also see the `Bytes` parameter.
    """

    type = unicode
    type_error = _('must be Unicode text')

    def __init__(self, name, *rules, **kw):
        if kw.get('pattern', None) is None:
            self.re = None
        else:
            self.re = re.compile(kw['pattern'], re.UNICODE)
        self.re_errmsg = kw.get('pattern_errmsg', None)
        super(Str, self).__init__(name, *rules, **kw)

    def _convert_scalar(self, value, index=None):
        """
        Convert a single scalar value.
        """
        if type(value) is self.type:
            return value
        if type(value) in (int, float):
            return self.type(value)
        if type(value) in (tuple, list):
            raise ConversionError(name=self.name, index=index,
            error=ugettext(self.scalar_error))
        raise ConversionError(name=self.name, index=index,
            error=ugettext(self.type_error),
        )

    def _rule_minlength(self, _, value):
        """
        Check minlength constraint.
        """
        assert type(value) is unicode
        if len(value) < self.minlength:
            return _('must be at least %(minlength)d characters') % dict(
                minlength=self.minlength,
            )

    def _rule_maxlength(self, _, value):
        """
        Check maxlength constraint.
        """
        assert type(value) is unicode
        if len(value) > self.maxlength:
            return _('can be at most %(maxlength)d characters') % dict(
                maxlength=self.maxlength,
            )

    def _rule_length(self, _, value):
        """
        Check length constraint.
        """
        assert type(value) is unicode
        if len(value) != self.length:
            return _('must be exactly %(length)d characters') % dict(
                length=self.length,
            )


class IA5Str(Str):
    """
    An IA5String per RFC 4517
    """

    def __init__(self, name, *rules, **kw):
        super(IA5Str, self).__init__(name, *rules, **kw)

    def _convert_scalar(self, value, index=None):
        if isinstance(value, basestring):
            for i in xrange(len(value)):
                if ord(value[i]) > 127:
                    raise ConversionError(name=self.name, index=index,
                        error=_('The character \'%(char)r\' is not allowed.') %
                            dict(char=value[i],)
                    )
        return super(IA5Str, self)._convert_scalar(value, index)


class Password(Str):
    """
    A parameter for passwords (stored in the ``unicode`` type).
    """

    def _convert_scalar(self, value, index=None):
        if isinstance(value, (tuple, list)) and len(value) == 2:
            (p1, p2) = value
            if p1 != p2:
                raise PasswordMismatch(name=self.name, index=index)
            value = p1
        return super(Password, self)._convert_scalar(value, index)


class Enum(Param):
    """
    Base class for parameters with enumerable values.
    """

    kwargs = Param.kwargs + (
        ('values', tuple, tuple()),
    )

    def __init__(self, name, *rules, **kw):
        super(Enum, self).__init__(name, *rules, **kw)
        for (i, v) in enumerate(self.values):
            if type(v) is not self.type:
                n = '%s values[%d]' % (self.nice, i)
                raise TypeError(
                    TYPE_ERROR % (n, self.type, v, type(v))
                )

    def _rule_values(self, _, value, **kw):
        if value not in self.values:
            return _('must be one of %(values)r') % dict(
                values=self.values,
            )


class BytesEnum(Enum):
    """
    Enumerable for binary data (stored in the ``str`` type).
    """

    type = unicode


class StrEnum(Enum):
    """
    Enumerable for Unicode text (stored in the ``unicode`` type).

    For example:

    >>> enum = StrEnum('my_enum', values=(u'One', u'Two', u'Three'))
    >>> enum.validate(u'Two', 'cli') is None
    True
    >>> enum.validate(u'Four', 'cli')
    Traceback (most recent call last):
      ...
    ValidationError: invalid 'my_enum': must be one of (u'One', u'Two', u'Three')
    """

    type = unicode


class List(Param):
    """
    Base class for parameters as a list of values. The input is a delimited
    string.
    """
    type = tuple

    kwargs = Param.kwargs + (
        ('separator', str, ','),
        ('skipspace', bool, True),
    )

    # The following 2 functions were taken from the Python
    # documentation at http://docs.python.org/library/csv.html
    def __utf_8_encoder(self, unicode_csv_data):
        for line in unicode_csv_data:
            yield line.encode('utf-8')

    def __unicode_csv_reader(self, unicode_csv_data, dialect=csv.excel, **kwargs):
        # csv.py doesn't do Unicode; encode temporarily as UTF-8:
        csv_reader = csv.reader(self.__utf_8_encoder(unicode_csv_data),
                                dialect=dialect, delimiter=self.separator,
                                skipinitialspace=self.skipspace,
                                **kwargs)
        for row in csv_reader:
            # decode UTF-8 back to Unicode, cell by cell:
            yield [unicode(cell, 'utf-8') for cell in row]

    def __init__(self, name, *rules, **kw):
        kw['multivalue'] = True
        super(List, self).__init__(name, *rules, **kw)

    def normalize(self, value):
        if value and not isinstance(value, tuple):
            reader = self.__unicode_csv_reader([value])
            value = []
            for row in reader:
                value = value + row
            value = tuple(value)
        return super(List, self).normalize(value)

    def _convert_scalar(self, value, index=None):
        return value

    def _validate_scalar(self, value, index=None):
        for rule in self.all_rules:
            error = rule(ugettext, value)
            if error is not None:
                raise ValidationError(
                    name=self.name,
                    value=value,
                    index=index,
                    error=error,
                    rule=rule,
                )


class File(Str):
    """
    File parameter type.

    Accepts file names and loads their content into the parameter value.
    """
    kwargs = Str.kwargs + (
        # valid for CLI, other backends (e.g. webUI) can ignore this
        ('stdin_if_missing', bool, False),
    )


class AccessTime(Str):
    """
    Access time parameter type.

    Accepts values conforming to generalizedTime as defined in RFC 4517
    section 3.3.13 without time zone information.
    """
    def _check_HHMM(self, t):
        if len(t) != 4:
            raise ValueError('HHMM must be exactly 4 characters long')
        if not t.isnumeric():
            raise ValueError('HHMM non-numeric')
        hh = int(t[0:2])
        if hh < 0 or hh > 23:
            raise ValueError('HH out of range')
        mm = int(t[2:4])
        if mm < 0 or mm > 59:
            raise ValueError('MM out of range')

    def _check_dotw(self, t):
        if t.isnumeric():
            value = int(t)
            if value < 1 or value > 7:
                raise ValueError('day of the week out of range')
        elif t not in ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'):
            raise ValueError('invalid day of the week')

    def _check_dotm(self, t, month_num=1, year=4):
        if not t.isnumeric():
            raise ValueError('day of the month non-numeric')
        value = int(t)
        if month_num in (1, 3, 5, 7, 8, 10, 12):
            if value < 1 or value > 31:
                raise ValueError('day of the month out of range')
        elif month_num in (4, 6, 9, 11):
            if value < 1 or value > 30:
                raise ValueError('day of the month out of range')
        elif month_num == 2:
            if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
                if value < 1 or value > 29:
                    raise ValueError('day of the month out of range')
            else:
                if value < 1 or value > 28:
                    raise ValueError('day of the month out of range')

    def _check_wotm(self, t):
        if not t.isnumeric():
            raise ValueError('week of the month non-numeric')
        value = int(t)
        if value < 1 or value > 6:
            raise ValueError('week of the month out of range')

    def _check_woty(self, t):
        if not t.isnumeric():
            raise ValueError('week of the year non-numeric')
        value = int(t)
        if value < 1 or value > 52:
            raise ValueError('week of the year out of range')

    def _check_doty(self, t):
        if not t.isnumeric():
            raise ValueError('day of the year non-numeric')
        value = int(t)
        if value < 1 or value > 365:
            raise ValueError('day of the year out of range')

    def _check_month_num(self, t):
        if not t.isnumeric():
            raise ValueError('month number non-numeric')
        value = int(t)
        if value < 1 or value > 12:
            raise ValueError('month number out of range')

    def _check_interval(self, t, check_func):
        intervals = t.split(',')
        for i in intervals:
            if not i:
                raise ValueError('invalid time range')
            values = i.split('-')
            if len(values) > 2:
                raise ValueError('invalid time range')
            for v in values:
                check_func(v)
            if len(values) == 2:
                if int(values[0]) > int(values[1]):
                    raise ValueError('invalid time range')

    def _check_W_spec(self, ts, index):
        if ts[index] != 'day':
            raise ValueError('invalid week specifier')
        index += 1
        self._check_interval(ts[index], self._check_dotw)
        return index

    def _check_M_spec(self, ts, index):
        if ts[index] == 'week':
            self._check_interval(ts[index + 1], self._check_wotm)
            index = self._check_W_spec(ts, index + 2)
        elif ts[index] == 'day':
            index += 1
            self._check_interval(ts[index], self._check_dotm)
        else:
            raise ValueError('invalid month specifier')
        return index

    def _check_Y_spec(self, ts, index):
        if ts[index] == 'month':
            index += 1
            self._check_interval(ts[index], self._check_month_num)
            month_num = int(ts[index])
            index = self._check_M_spec(ts, index + 1)
        elif ts[index] == 'week':
            self._check_interval(ts[index + 1], self._check_woty)
            index = self._check_W_spec(ts, index + 2)
        elif ts[index] == 'day':
            index += 1
            self._check_interval(ts[index], self._check_doty)
        else:
            raise ValueError('invalid year specifier')
        return index

    def _check_generalized(self, t):
        assert type(t) is unicode
        if len(t) not in (10, 12, 14):
            raise ValueError('incomplete generalized time')
        if not t.isnumeric():
            raise ValueError('time non-numeric')
        # don't check year value, with time travel and all :)
        self._check_month_num(t[4:6])
        year_num = int(t[0:4])
        month_num = int(t[4:6])
        self._check_dotm(t[6:8], month_num, year_num)
        if len(t) >= 12:
            self._check_HHMM(t[8:12])
        else:
            self._check_HHMM('%s00' % t[8:10])
        if len(t) == 14:
            s = int(t[12:14])
            if s < 0 or s > 60:
                raise ValueError('seconds out of range')

    def _check(self, time):
        ts = time.split()
        if ts[0] == 'absolute':
            if len(ts) != 4:
                raise ValueError('invalid format, must be \'absolute generalizedTime ~ generalizedTime\'')
            self._check_generalized(ts[1])
            if ts[2] != '~':
                raise ValueError('invalid time range separator')
            self._check_generalized(ts[3])
            if int(ts[1]) >= int(ts[3]):
                raise ValueError('invalid time range')
        elif ts[0] == 'periodic':
            index = None
            if ts[1] == 'yearly':
                index = self._check_Y_spec(ts, 2)
            elif ts[1] == 'monthly':
                index = self._check_M_spec(ts, 2)
            elif ts[1] == 'weekly':
                index = self._check_W_spec(ts, 2)
            elif ts[1] == 'daily':
                index = 1
            if index is None:
                raise ValueError('period must be yearly, monthy or daily, got \'%s\'' % ts[1])
            self._check_interval(ts[index + 1], self._check_HHMM)
        else:
            raise ValueError('time neither absolute or periodic')

    def _rule_required(self, _, value):
        try:
            self._check(value)
        except ValueError, e:
            raise ValidationError(name=self.cli_name, error=e.args[0])
        except IndexError:
            raise ValidationError(
                name=self.cli_name, error='incomplete time value'
            )
        return None


def create_param(spec):
    """
    Create an `Str` instance from the shorthand ``spec``.

    This function allows you to create `Str` parameters (the most common) from
    a convenient shorthand that defines the parameter name, whether it is
    required, and whether it is multivalue.  (For the definition of the
    shorthand syntax, see the `parse_param_spec()` function.)

    If ``spec`` is an ``str`` instance, it will be used to create a new `Str`
    parameter, which will be returned.  For example:

    >>> s = create_param('hometown?')
    >>> s
    Str('hometown?')
    >>> (s.name, s.required, s.multivalue)
    ('hometown', False, False)

    On the other hand, if ``spec`` is already a `Param` instance, it is
    returned unchanged.  For example:

    >>> b = Bytes('cert')
    >>> create_param(b) is b
    True

    As a plugin author, you will not call this function directly (which would
    be no more convenient than simply creating the `Str` instance).  Instead,
    `frontend.Command` will call it for you when it evaluates the
    ``takes_args`` and ``takes_options`` attributes, and `frontend.Object`
    will call it for you when it evaluates the ``takes_params`` attribute.

    :param spec: A spec string or a `Param` instance.
    """
    if isinstance(spec, Param):
        return spec
    if type(spec) is not str:
        raise TypeError(
            TYPE_ERROR % ('spec', (str, Param), spec, type(spec))
        )
    return Str(spec)
