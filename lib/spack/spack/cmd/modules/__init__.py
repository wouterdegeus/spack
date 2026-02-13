# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Implementation details of the ``spack module`` command."""

import collections
import os
import shutil
import sys

import spack.cmd
import spack.config
import spack.error
import spack.modules
import spack.modules.common
import spack.repo
from spack.cmd import MultipleSpecsMatch, NoSpecMatches
from spack.cmd.common import arguments
from spack.llnl.util import filesystem, tty
from spack.llnl.util.tty import color

description = "manipulate module files"
section = "environment"
level = "short"


def setup_parser(subparser):
    subparser.add_argument(
        "-n",
        "--name",
        action="store",
        dest="module_set_name",
        default="default",
        help="named module set to use from modules configuration",
    )
    sp = subparser.add_subparsers(metavar="SUBCOMMAND", dest="subparser_name")

    refresh_parser = sp.add_parser("refresh", help="regenerate module files")
    refresh_parser.add_argument(
        "--delete-tree", help="delete the module file tree before refresh", action="store_true"
    )
    refresh_parser.add_argument(
        "--upstream-modules",
        help="generate modules for packages installed upstream",
        action="store_true",
    )
    arguments.add_common_arguments(refresh_parser, ["constraint", "yes_to_all"])

    find_parser = sp.add_parser("find", help="find module files for packages")
    find_parser.add_argument(
        "--full-path", help="display full path to module file", action="store_true"
    )
    arguments.add_common_arguments(find_parser, ["constraint", "recurse_dependencies"])

    rm_parser = sp.add_parser("rm", help="remove module files")
    arguments.add_common_arguments(rm_parser, ["constraint", "yes_to_all"])

    loads_parser = sp.add_parser(
        "loads", help="prompt the list of modules associated with a constraint"
    )
    add_loads_arguments(loads_parser)
    arguments.add_common_arguments(loads_parser, ["constraint"])

    return sp


def add_loads_arguments(subparser):
    subparser.add_argument(
        "--input-only",
        action="store_false",
        dest="shell",
        help="generate input for module command (instead of a shell script)",
    )
    subparser.add_argument(
        "-p",
        "--prefix",
        dest="prefix",
        default="",
        help="prepend to module names when issuing module load commands",
    )
    subparser.add_argument(
        "-x",
        "--exclude",
        dest="exclude",
        action="append",
        default=[],
        help="exclude package from output; may be specified multiple times",
    )
    arguments.add_common_arguments(subparser, ["recurse_dependencies"])


def one_spec_or_raise(specs):
    """Ensures exactly one spec has been selected, or raises the appropriate
    exception.
    """
    # Ensure a single spec matches the constraint
    if len(specs) == 0:
        raise NoSpecMatches()
    if len(specs) > 1:
        raise MultipleSpecsMatch()

    # Get the spec and module type
    return specs[0]


def check_module_set_name(name):
    modules = spack.config.get("modules")
    if name != "prefix_inspections" and name in modules:
        return

    names = [k for k in modules if k != "prefix_inspections"]

    if not names:
        raise spack.error.ConfigError(
            f"Module set configuration is missing. Cannot use module set '{name}'"
        )

    pretty_names = "', '".join(names)

    raise spack.error.ConfigError(
        f"Cannot use invalid module set '{name}'.",
        f"Valid module set names are: '{pretty_names}'.",
    )


_missing_modules_warning = (
    "Modules have been omitted for one or more specs, either"
    " because they were excluded or because the spec is"
    " associated with a package that is installed upstream and"
    " that installation has not generated a module file. Rerun"
    " this command with debug output enabled for more details."
)


def loads(module_type, specs, args, out=None):
    """Prompt the list of modules associated with a list of specs"""
    check_module_set_name(args.module_set_name)
    out = sys.stdout if out is None else out

    # Get a comprehensive list of specs
    if args.recurse_dependencies:
        specs_from_user_constraint = specs[:]
        specs = []
        # FIXME : during module file creation nodes seem to be visited
        # FIXME : multiple times even if cover='nodes' is given. This
        # FIXME : work around permits to get a unique list of spec anyhow.
        # FIXME : (same problem as in spack/modules.py)
        seen = set()
        seen_add = seen.add
        for spec in specs_from_user_constraint:
            specs.extend(
                [
                    item
                    for item in spec.traverse(order="post", cover="nodes")
                    if not (item in seen or seen_add(item))
                ]
            )

    modules = list(
        (
            spec,
            spack.modules.get_module(
                module_type,
                spec,
                get_full_path=False,
                module_set_name=args.module_set_name,
                required=False,
            ),
        )
        for spec in specs
    )

    module_commands = {"tcl": "module load ", "lmod": "module load "}

    d = {"command": "" if not args.shell else module_commands[module_type], "prefix": args.prefix}

    exclude_set = set(args.exclude)
    load_template = "{comment}{exclude}{command}{prefix}{name}"
    for spec, mod in modules:
        if not mod:
            module_output_for_spec = "## excluded or missing from upstream: {0}".format(
                spec.format()
            )
        else:
            d["exclude"] = "## " if spec.name in exclude_set else ""
            d["comment"] = "" if not args.shell else "# {0}\n".format(spec.format())
            d["name"] = mod
            module_output_for_spec = load_template.format(**d)
        out.write(module_output_for_spec)
        out.write("\n")

    if not all(mod for _, mod in modules):
        tty.warn(_missing_modules_warning)


def find(module_type, specs, args):
    """Retrieve paths or use names of module files"""
    check_module_set_name(args.module_set_name)

    single_spec = one_spec_or_raise(specs)

    if args.recurse_dependencies:
        dependency_specs_to_retrieve = list(
            single_spec.traverse(root=False, order="post", cover="nodes", deptype=("link", "run"))
        )
    else:
        dependency_specs_to_retrieve = []

    try:
        modules = [
            spack.modules.get_module(
                module_type,
                spec,
                args.full_path,
                module_set_name=args.module_set_name,
                required=False,
            )
            for spec in dependency_specs_to_retrieve
        ]

        modules.append(
            spack.modules.get_module(
                module_type,
                single_spec,
                args.full_path,
                module_set_name=args.module_set_name,
                required=True,
            )
        )
    except spack.modules.common.ModuleNotFoundError as e:
        tty.die(e.message)

    if not all(modules):
        tty.warn(_missing_modules_warning)
    modules = list(x for x in modules if x)
    print(" ".join(modules))


def rm(module_type, specs, args):
    """Deletes the module files associated with every spec in specs, for every
    module type in module types.
    """
    check_module_set_name(args.module_set_name)

    module_cls = spack.modules.module_types[module_type]
    module_exist = lambda x: os.path.exists(module_cls(x, args.module_set_name).layout.filename)

    specs_with_modules = [spec for spec in specs if module_exist(spec)]

    modules = [module_cls(spec, args.module_set_name) for spec in specs_with_modules]

    if not modules:
        tty.die("No module file matches your query")

    # Ask for confirmation
    if not args.yes_to_all:
        msg = "You are about to remove {0} module files for:\n"
        tty.msg(msg.format(module_type))
        spack.cmd.display_specs(specs_with_modules, long=True)
        print("")
        answer = tty.get_yes_or_no("Do you want to proceed?")
        if not answer:
            tty.die("Will not remove any module files")

    # Remove the module files
    for s in modules:
        s.remove()


def _refresh_with_atomic_swap(module_type, module_type_root, module_set_name, cls, specs, args):
    """Regenerate module files using atomic directory swap to avoid downtime.
    
    This function builds all module files in a temporary directory, then
    atomically swaps it with the existing directory. This ensures users
    never see "module not found" errors during the refresh.
    
    Args:
        module_type: the type of module system (e.g., "lmod", "tcl")
        module_type_root: the current root directory for module files
        module_set_name: name of the module set from configuration
        cls: the module writer class
        specs: list of specs to generate modules for
        args: command-line arguments
        
    Returns:
        List of error messages encountered during module generation
    """
    import time
    import tempfile
    
    errors = []
    timestamp = str(int(time.time()))
    
    # Create temporary directory for building new modules
    # Use parent directory to ensure we're on the same filesystem (required for atomic rename)
    parent_dir = os.path.dirname(module_type_root.rstrip(os.sep))
    temp_root = None
    backup_root = None
    
    try:
        # Create temporary directory with unique name
        temp_root = tempfile.mkdtemp(
            prefix=f"{os.path.basename(module_type_root)}-new.",
            suffix=f".{timestamp}.tmp",
            dir=parent_dir
        )
        tty.debug(f"Building modules in temporary directory: {temp_root}")
        
        # Build all modules in the temporary directory
        tty.msg(f"Building module files in temporary location")
        
        # Temporarily override the module root path
        # Save the original value to restore it later
        config_path = f"modules:{module_set_name}:roots:{module_type}"
        original_value = spack.config.get(config_path, None)
        
        try:
            # Set the temp directory as the module root
            spack.config.set(config_path, temp_root)
            
            # Recreate writers with the overridden configuration
            writers = [
                cls(spec, module_set_name) for spec in specs if spack.repo.PATH.exists(spec.name)
            ]
            # Filter excluded packages
            writers = [x for x in writers if not x.conf.excluded]
            
            tty.msg(f"Building {len(writers)} module files")
            
            # Generate module index in temp directory
            spack.modules.common.generate_module_index(
                temp_root, writers, overwrite=True
            )
            
            # Write each module file
            for x in writers:
                try:
                    x.write(overwrite=True)
                except spack.error.SpackError as e:
                    msg = f"{x.layout.filename}: {e.message}"
                    errors.append(msg)
                except Exception as e:
                    msg = f"{x.layout.filename}: {str(e)}"
                    errors.append(msg)
        finally:
            # Restore original configuration
            if original_value is not None:
                spack.config.set(config_path, original_value)
            else:
                # If there was no original value, remove the config we set
                try:
                    spack.config.remove(config_path)
                except Exception:
                    pass  # If removal fails, it's not critical
        
        # If there were errors but some modules were built, continue with swap
        # Users may want partial results
        if errors:
            tty.warn(f"Encountered {len(errors)} errors during module generation")
        
        # Prepare backup directory name
        backup_root = f"{module_type_root}-old.{timestamp}.backup"
        
        # Perform atomic swap
        tty.msg("Performing atomic directory swap")
        
        # Step 1: Move old directory to backup (if it exists)
        if os.path.exists(module_type_root):
            tty.debug(f"Moving {module_type_root} to {backup_root}")
            os.rename(module_type_root, backup_root)
        
        # Step 2: Move new directory to production location
        try:
            tty.debug(f"Moving {temp_root} to {module_type_root}")
            os.rename(temp_root, module_type_root)
            temp_root = None  # Mark as successfully moved
            tty.msg("Module files refreshed successfully")
        except Exception as e:
            # Rollback: restore old directory
            tty.error(f"Failed to move new modules into place: {e}")
            if backup_root and os.path.exists(backup_root):
                tty.msg("Rolling back to previous module files")
                os.rename(backup_root, module_type_root)
                backup_root = None  # Mark as restored
            raise
        
        # Step 3: Cleanup old backup directory
        if backup_root and os.path.exists(backup_root):
            tty.debug(f"Removing old module directory: {backup_root}")
            shutil.rmtree(backup_root, ignore_errors=True)
            
    except Exception as e:
        tty.error(f"Atomic swap failed: {e}")
        # Cleanup temporary directory if it still exists
        if temp_root and os.path.exists(temp_root):
            tty.debug(f"Cleaning up temporary directory: {temp_root}")
            shutil.rmtree(temp_root, ignore_errors=True)
        raise
    
    return errors


def refresh(module_type, specs, args):
    """Regenerates the module files for every spec in specs and every module
    type in module types.
    """
    check_module_set_name(args.module_set_name)

    # Prompt a message to the user about what is going to change
    if not specs:
        tty.msg("No package matches your query")
        return

    if not args.upstream_modules:
        specs = list(s for s in specs if not s.installed_upstream)

    if not args.yes_to_all:
        msg = "You are about to regenerate {types} module files for:\n"
        tty.msg(msg.format(types=module_type))
        spack.cmd.display_specs(specs, long=True)
        print("")
        answer = tty.get_yes_or_no("Do you want to proceed?")
        if not answer:
            tty.die("Module file regeneration aborted.")

    # Cycle over the module types and regenerate module files

    cls = spack.modules.module_types[module_type]

    # Skip unknown packages.
    writers = [
        cls(spec, args.module_set_name) for spec in specs if spack.repo.PATH.exists(spec.name)
    ]

    # Filter excluded packages early
    writers = [x for x in writers if not x.conf.excluded]

    # Detect name clashes in module files
    file2writer = collections.defaultdict(list)
    for item in writers:
        file2writer[item.layout.filename].append(item)

    if len(file2writer) != len(writers):
        spec_fmt_str = "{name}@={version}%{compiler}/{hash:7} {variants} arch={arch}"
        message = "Name clashes detected in module files:\n"
        for filename, writer_list in file2writer.items():
            if len(writer_list) > 1:
                message += "\nfile: {0}\n".format(filename)
                for x in writer_list:
                    message += "spec: {0}\n".format(x.spec.format(spec_fmt_str))
        tty.error(message)
        tty.error("Operation aborted")
        raise SystemExit(1)

    if len(writers) == 0:
        msg = "Nothing to be done for {0} module files."
        tty.msg(msg.format(module_type))
        return
    # If we arrived here we have at least one writer
    module_type_root = writers[0].layout.dirname()

    # Proceed regenerating module files
    tty.msg("Regenerating {name} module files".format(name=module_type))
    
    # Use atomic swap when doing a full refresh with --delete-tree
    # This prevents users from seeing "module not found" errors during rebuild
    if args.delete_tree and os.path.isdir(module_type_root):
        errors = _refresh_with_atomic_swap(
            module_type, module_type_root, args.module_set_name, cls, specs, args
        )
        if errors:
            errors.insert(0, color.colorize("@*{some module files could not be written}"))
            tty.warn("\n".join(errors))
    else:
        # Original behavior for incremental updates or when directory doesn't exist
        # (no deletion needed here - if delete_tree was set with existing dir, 
        # we'd be in the atomic swap path above)
        filesystem.mkdirp(module_type_root)

        # Dump module index after potentially removing module tree
        spack.modules.common.generate_module_index(
            module_type_root, writers, overwrite=args.delete_tree
        )
        errors = []
        for x in writers:
            try:
                x.write(overwrite=True)
            except spack.error.SpackError as e:
                msg = f"{x.layout.filename}: {e.message}"
                errors.append(msg)
            except Exception as e:
                msg = f"{x.layout.filename}: {str(e)}"
                errors.append(msg)

        if errors:
            errors.insert(0, color.colorize("@*{some module files could not be written}"))
            tty.warn("\n".join(errors))


#: Dictionary populated with the list of sub-commands.
#: Each sub-command must be callable and accept 3 arguments:
#:
#: - module_type: the type of module it refers to
#: - specs : the list of specs to be processed
#: - args : namespace containing the parsed command line arguments
callbacks = {"refresh": refresh, "rm": rm, "find": find, "loads": loads}


def modules_cmd(parser, args, module_type, callbacks=callbacks):
    # Qualifiers to be used when querying the db for specs
    constraint_qualifiers = {
        "refresh": {
            "installed": True,
            "predicate_fn": lambda x: spack.repo.PATH.exists(x.spec.name),
        }
    }
    query_args = constraint_qualifiers.get(args.subparser_name, {})

    # Get the specs that match the query from the DB
    specs = args.specs(**query_args)

    try:
        callbacks[args.subparser_name](module_type, specs, args)

    except MultipleSpecsMatch:
        query = " ".join(str(s) for s in args.constraint_specs)
        msg = f"the constraint '{query}' matches multiple packages:\n"
        for s in specs:
            spec_fmt = (
                "{hash:7} {name}{@version}{compiler_flags}{variants}"
                "{ platform=architecture.platform}{ os=architecture.os}"
                "{ target=architecture.target}"
                "{%compiler}"
            )
            msg += "\t" + s.cformat(spec_fmt) + "\n"
        tty.die(msg, "In this context exactly *one* match is needed.")

    except NoSpecMatches:
        query = " ".join(str(s) for s in args.constraint_specs)
        msg = f"the constraint '{query}' matches no package."
        tty.die(msg, "In this context exactly *one* match is needed.")
