from __future__ import absolute_import

import logging
import os
from collections import defaultdict

from pip.compat import expanduser
from pip.exceptions import InstallationError
from pip.utils import display_path, normalize_path
from pip.utils.logging import indent_log
from pip.wheel import Wheel

logger = logging.getLogger(__name__)


class Requirements(object):

    def __init__(self):
        self._keys = []
        self._dict = {}

    def keys(self):
        return self._keys

    def values(self):
        return [self._dict[key] for key in self._keys]

    def __contains__(self, item):
        return item in self._keys

    def __setitem__(self, key, value):
        if key not in self._keys:
            self._keys.append(key)
        self._dict[key] = value

    def __getitem__(self, key):
        return self._dict[key]

    def __repr__(self):
        values = ['%s: %s' % (repr(k), repr(self[k])) for k in self.keys()]
        return 'Requirements({%s})' % ', '.join(values)


class RequirementSet(object):

    def __init__(self, build_dir, src_dir, download_dir=None, upgrade=False,
                 upgrade_strategy=None, ignore_installed=False,
                 target_dir=None, ignore_dependencies=False,
                 force_reinstall=False, use_user_site=False, session=None,
                 pycompile=True, isolated=False, wheel_download_dir=None,
                 wheel_cache=None, require_hashes=False,
                 ignore_requires_python=False, progress_bar="on"):
        """Create a RequirementSet.

        :param wheel_download_dir: Where still-packed .whl files should be
            written to. If None they are written to the download_dir parameter.
            Separate to download_dir to permit only keeping wheel archives for
            pip wheel.
        :param download_dir: Where still packed archives should be written to.
            If None they are not saved, and are deleted immediately after
            unpacking.
        :param wheel_cache: The pip wheel cache, for passing to
            InstallRequirement.
        """
        if session is None:
            raise TypeError(
                "RequirementSet() missing 1 required keyword argument: "
                "'session'"
            )

        self.build_dir = build_dir
        self.src_dir = src_dir
        # XXX: download_dir and wheel_download_dir overlap semantically and may
        # be combined if we're willing to have non-wheel archives present in
        # the wheelhouse output by 'pip wheel'.
        self.download_dir = download_dir
        self.upgrade = upgrade
        self.upgrade_strategy = upgrade_strategy
        self.ignore_installed = ignore_installed
        self.force_reinstall = force_reinstall
        self.requirements = Requirements()
        # Mapping of alias: real_name
        self.requirement_aliases = {}
        self.unnamed_requirements = []
        self.ignore_dependencies = ignore_dependencies
        self.ignore_requires_python = ignore_requires_python
        self.progress_bar = progress_bar
        self.successfully_downloaded = []
        self.successfully_installed = []
        self.reqs_to_cleanup = []
        self.use_user_site = use_user_site
        self.target_dir = target_dir  # set from --target option
        self.session = session
        self.pycompile = pycompile
        self.isolated = isolated
        if wheel_download_dir:
            wheel_download_dir = normalize_path(wheel_download_dir)
        self.wheel_download_dir = wheel_download_dir
        self._wheel_cache = wheel_cache
        self.require_hashes = require_hashes
        # Maps from install_req -> dependencies_of_install_req
        self._dependencies = defaultdict(list)

    def __str__(self):
        reqs = [req for req in self.requirements.values()
                if not req.comes_from]
        reqs.sort(key=lambda req: req.name.lower())
        return ' '.join([str(req.req) for req in reqs])

    def __repr__(self):
        reqs = [req for req in self.requirements.values()]
        reqs.sort(key=lambda req: req.name.lower())
        reqs_str = ', '.join([str(req.req) for req in reqs])
        return ('<%s object; %d requirement(s): %s>'
                % (self.__class__.__name__, len(reqs), reqs_str))

    def add_requirement(self, install_req, parent_req_name=None,
                        extras_requested=None):
        """Add install_req as a requirement to install.

        :param parent_req_name: The name of the requirement that needed this
            added. The name is used because when multiple unnamed requirements
            resolve to the same name, we could otherwise end up with dependency
            links that point outside the Requirements set. parent_req must
            already be added. Note that None implies that this is a user
            supplied requirement, vs an inferred one.
        :param extras_requested: an iterable of extras used to evaluate the
            environement markers.
        :return: Additional requirements to scan. That is either [] if
            the requirement is not applicable, or [install_req] if the
            requirement is applicable and has just been added.
        """
        name = install_req.name
        if not install_req.match_markers(extras_requested):
            logger.warning("Ignoring %s: markers '%s' don't match your "
                           "environment", install_req.name,
                           install_req.markers)
            return []

        # This check has to come after we filter requirements with the
        # environment markers.
        if install_req.link and install_req.link.is_wheel:
            wheel = Wheel(install_req.link.filename)
            if not wheel.supported():
                raise InstallationError(
                    "%s is not a supported wheel on this platform." %
                    wheel.filename
                )

        install_req.use_user_site = self.use_user_site
        install_req.target_dir = self.target_dir
        install_req.pycompile = self.pycompile
        install_req.is_direct = (parent_req_name is None)

        if not name:
            # url or path requirement w/o an egg fragment
            self.unnamed_requirements.append(install_req)
            return [install_req]
        else:
            try:
                existing_req = self.get_requirement(name)
            except KeyError:
                existing_req = None
            if (parent_req_name is None and existing_req and not
                    existing_req.constraint and
                    existing_req.extras == install_req.extras and not
                    existing_req.req.specifier == install_req.req.specifier):
                raise InstallationError(
                    'Double requirement given: %s (already in %s, name=%r)'
                    % (install_req, existing_req, name))
            if not existing_req:
                # Add requirement
                self.requirements[name] = install_req
                # FIXME: what about other normalizations?  E.g., _ vs. -?
                if name.lower() != name:
                    self.requirement_aliases[name.lower()] = name
                result = [install_req]
            else:
                # Assume there's no need to scan, and that we've already
                # encountered this for scanning.
                result = []
                if not install_req.constraint and existing_req.constraint:
                    if (install_req.link and not (existing_req.link and
                       install_req.link.path == existing_req.link.path)):
                        self.reqs_to_cleanup.append(install_req)
                        raise InstallationError(
                            "Could not satisfy constraints for '%s': "
                            "installation from path or url cannot be "
                            "constrained to a version" % name)
                    # If we're now installing a constraint, mark the existing
                    # object for real installation.
                    existing_req.constraint = False
                    existing_req.extras = tuple(
                        sorted(set(existing_req.extras).union(
                               set(install_req.extras))))
                    logger.debug("Setting %s extras to: %s",
                                 existing_req, existing_req.extras)
                    # And now we need to scan this.
                    result = [existing_req]
                # Canonicalise to the already-added object for the backref
                # check below.
                install_req = existing_req
            if parent_req_name:
                parent_req = self.get_requirement(parent_req_name)
                self._dependencies[parent_req].append(install_req)
            return result

    def has_requirement(self, project_name):
        name = project_name.lower()
        return (
            name in self.requirements and
            not self.requirements[name].constraint or
            name in self.requirement_aliases and
            not self.requirements[self.requirement_aliases[name]].constraint
        )

    @property
    def has_requirements(self):
        return list(req for req in self.requirements.values() if not
                    req.constraint) or self.unnamed_requirements

    @property
    def is_download(self):
        if self.download_dir:
            self.download_dir = expanduser(self.download_dir)
            if os.path.exists(self.download_dir):
                return True
            else:
                logger.critical('Could not find download directory')
                raise InstallationError(
                    "Could not find or access download directory '%s'"
                    % display_path(self.download_dir))
        return False

    def get_requirement(self, project_name):
        for name in project_name, project_name.lower():
            if name in self.requirements:
                return self.requirements[name]
            if name in self.requirement_aliases:
                return self.requirements[self.requirement_aliases[name]]
        raise KeyError("No project with the name %r" % project_name)

    def cleanup_files(self):
        """Clean up files, remove builds."""
        logger.debug('Cleaning up...')
        with indent_log():
            for req in self.reqs_to_cleanup:
                req.remove_temporary_source()

    def _to_install(self):
        """Create the installation order.

        The installation order is topological - requirements are installed
        before the requiring thing. We break cycles at an arbitrary point,
        and make no other guarantees.
        """
        # The current implementation, which we may change at any point
        # installs the user specified things in the order given, except when
        # dependencies must come earlier to achieve topological order.
        order = []
        ordered_reqs = set()

        def schedule(req):
            if req.satisfied_by or req in ordered_reqs:
                return
            if req.constraint:
                return
            ordered_reqs.add(req)
            for dep in self._dependencies[req]:
                schedule(dep)
            order.append(req)
        for install_req in self.requirements.values():
            schedule(install_req)
        return order

    def install(self, install_options, global_options=(), *args, **kwargs):
        """
        Install everything in this set (after having downloaded and unpacked
        the packages)
        """
        to_install = self._to_install()

        if to_install:
            logger.info(
                'Installing collected packages: %s',
                ', '.join([req.name for req in to_install]),
            )

        with indent_log():
            for requirement in to_install:
                if requirement.conflicts_with:
                    logger.info(
                        'Found existing installation: %s',
                        requirement.conflicts_with,
                    )
                    with indent_log():
                        requirement.uninstall(auto_confirm=True)
                try:
                    requirement.install(
                        install_options,
                        global_options,
                        *args,
                        **kwargs
                    )
                except:
                    # if install did not succeed, rollback previous uninstall
                    if (requirement.conflicts_with and not
                            requirement.install_succeeded):
                        requirement.uninstalled_pathset.rollback()
                    raise
                else:
                    if (requirement.conflicts_with and
                            requirement.install_succeeded):
                        requirement.uninstalled_pathset.commit()
                requirement.remove_temporary_source()

        self.successfully_installed = to_install
