#!/usr/bin/python
# Copyright 2016 The Tulsi Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Bridge between Xcode and Bazel for the "build" action.

NOTE: This script must be executed in the same directory as the Xcode project's
main group in order to generate correct debug symbols.
"""

import collections
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import zipfile


class Timer(object):
  """Simple profiler."""

  def __init__(self, action_name):
    self.action_name = action_name

  def Start(self):
    self._start = time.time()
    return self

  def End(self):
    end = time.time()
    seconds = end - self._start
    print '<*> %s completed in %0.3f ms' % (self.action_name, seconds * 1000)


class _OptionsParser(object):
  """Handles parsing script options."""

  # Key for options that should be applied to all build configurations.
  ALL_CONFIGS = '__all__'

  # The build configurations handled by this parser.
  KNOWN_CONFIGS = ['Debug', 'Release', 'Fastbuild']

  def __init__(self, sdk_version, arch, main_group_path):
    self.targets = []
    self.startup_options = collections.defaultdict(list)
    self.build_options = collections.defaultdict(
        list,
        {
            _OptionsParser.ALL_CONFIGS: [
                '--experimental_enable_objc_cc_deps',
                '--verbose_failures',
            ],

            'Debug': [
                '--compilation_mode=dbg',
                '--copt=-g',
                '--copt=-Xclang', '--copt=-fdebug-compilation-dir',
                '--copt=-Xclang', '--copt=%s' % main_group_path,
                '--objccopt=-Xclang', '--objccopt=-fdebug-compilation-dir',
                '--objccopt=-Xclang', '--objccopt=%s' % main_group_path,
                '--objc_generate_debug_symbols',
            ],

            'Release': [
                '--compilation_mode=opt',
                '--objc_generate_debug_symbols',
                '--strip=always',
            ],

            'Fastbuild': [
                '--compilation_mode=fastbuild',
            ],
        })

    self.sdk_version = sdk_version

    if arch:
      self.build_options[_OptionsParser.ALL_CONFIGS].append(
          '--config=ios_' + arch)

    ios_minimum_os = os.environ.get('IPHONEOS_DEPLOYMENT_TARGET', None)
    if ios_minimum_os:
      self.build_options[_OptionsParser.ALL_CONFIGS].append(
          '--ios_minimum_os=' + ios_minimum_os)

    self.verbose = True
    self.install_generated_artifacts = False

  def _UsageMessage(self):
    """Returns a usage message string."""
    usage = textwrap.dedent("""\
      Usage: %s <target> [<target2> ...] --bazel <bazel_binary_path> [options]

      Where options are:
        --noverbose
            Disables verbose script output.

        --unpack_generated_ipa
            Unzips the contents of the IPA artifact generated by this build.

        --bazel_startup_options <option1> [<option2> ...] --
            Provides one or more Bazel startup options.

        --bazel_options <option1> [<option2> ...] --
            Provides one or more Bazel build options.
      """ % sys.argv[0])

    usage += '\n' + textwrap.fill(
        'Note that the --bazel_startup_options and --bazel_options options may '
        'include an optional configuration specifier in brackets to limit '
        'their contents to a given build configuration. Options provided with '
        'no configuration filter will apply to all configurations in addition '
        'to any configuration-specific options.', 120)

    usage += '\n' + textwrap.fill(
        'E.g., --bazel_options common --  --bazel_options[Release] release -- '
        'would result in "bazel build common release" in the "Release" '
        'configuration and "bazel build common" in all other configurations.',
        120)

    return usage

  def ParseOptions(self, args):
    """Parses arguments, returning (message, exit_code)."""

    bazel_executable_index = args.index('--bazel')

    self.targets = args[:bazel_executable_index]
    if not self.targets or len(args) < bazel_executable_index + 2:
      return (self._UsageMessage(), 10)
    self.bazel_executable = args[bazel_executable_index + 1]

    return self._ParseVariableOptions(args[bazel_executable_index + 2:])

  def GetStartupOptions(self, config):
    """Returns the full set of startup options for the given config."""
    return self._GetOptions(self.startup_options, config)

  def GetBuildOptions(self, config):
    """Returns the full set of build options for the given config."""
    options = self._GetOptions(self.build_options, config)

    version_string = self._GetXcodeVersionString()
    if version_string:
      self._AddDefaultOption(options, '--xcode_version', version_string)

    if self.sdk_version:
      self._AddDefaultOption(options, '--ios_sdk_version', self.sdk_version)
    return options

  @staticmethod
  def _AddDefaultOption(option_list, option, default_value):
    matching_options = [opt for opt in option_list if opt.startswith(option)]
    if matching_options:
      return option_list

    option_list.append('%s=%s' % (option, default_value))
    return option_list

  @staticmethod
  def _GetOptions(option_set, config):
    """Returns a flattened list from options_set for the given config."""
    options = list(option_set[_OptionsParser.ALL_CONFIGS])
    if config != _OptionsParser.ALL_CONFIGS:
      options.extend(option_set[config])
    return options

  def _ParseVariableOptions(self, args):
    """Parses flag-based args, returning (message, exit_code)."""

    while args:
      arg = args[0]
      args = args[1:]

      if arg == '--noverbose':
        self.verbose = False

      elif arg == '--install_generated_artifacts':
        self.install_generated_artifacts = True

      elif arg.startswith('--bazel_startup_options'):
        config = self._ParseConfigFilter(arg)
        args, items, terminated = self._ParseDoubleDashDelimitedItems(args)
        if not terminated:
          return (('Missing "--" terminator while parsing %s' % arg),
                  2)
        duplicates = self._FindDuplicateOptions(self.startup_options,
                                                config,
                                                items)
        if duplicates:
          return (
              '%s items conflict with common options: %s' % (
                  arg, ','.join(duplicates)),
              2)
        self.startup_options[config].extend(items)

      elif arg.startswith('--bazel_options'):
        config = self._ParseConfigFilter(arg)
        args, items, terminated = self._ParseDoubleDashDelimitedItems(args)
        if not terminated:
          return ('Missing "--" terminator while parsing %s' % arg, 2)
        duplicates = self._FindDuplicateOptions(self.build_options,
                                                config,
                                                items)
        if duplicates:
          return (
              '%s items conflict with common options: %s' % (
                  arg, ','.join(duplicates)),
              2)
        self.build_options[config].extend(items)

      else:
        return ('Unknown option "%s"\n%s' % (arg, self._UsageMessage()), 1)

    return (None, 0)

  @staticmethod
  def _ParseConfigFilter(arg):
    match = re.search(r'\[([^\]]+)\]', arg)
    if not match:
      return _OptionsParser.ALL_CONFIGS
    return match.group(1)

  @staticmethod
  def _ConsumeArgumentForParam(param, args):
    if not args:
      return (None, 'Missing required parameter for "%s" option' % param)
    val = args[0]
    return (args[1:], val)

  @staticmethod
  def _ParseDoubleDashDelimitedItems(args):
    """Consumes options until -- is found."""
    options = []
    terminator_found = False

    opts = args
    while opts:
      opt = opts[0]
      opts = opts[1:]
      if opt == '--':
        terminator_found = True
        break
      options.append(opt)

    return opts, options, terminator_found

  @staticmethod
  def _FindDuplicateOptions(options_dict, config, new_options):
    """Returns a list of options appearing in both given option lists."""

    allowed_duplicates = [
        '--copt',
        '--config',
        '--define',
    ]

    def ExtractOptionNames(opts):
      names = set()
      for opt in opts:
        split_opt = opt.split('=', 1)
        if split_opt[0] not in allowed_duplicates:
          names.add(split_opt[0])
      return names

    current_set = ExtractOptionNames(options_dict[config])
    new_set = ExtractOptionNames(new_options)
    conflicts = current_set.intersection(new_set)

    if config != _OptionsParser.ALL_CONFIGS:
      current_set = ExtractOptionNames(options_dict[_OptionsParser.ALL_CONFIGS])
      conflicts = conflicts.union(current_set.intersection(new_set))
    return conflicts

  def _GetXcodeVersionString(self):
    """Returns Xcode version info from the environment as a string."""
    reported_version = os.environ['XCODE_VERSION_ACTUAL']
    match = re.match(r'(\d{2})(\d)(\d)$', reported_version)
    if not match:
      self._PrintVerbose(
          'Failed to extract Xcode version from %s' % reported_version)
      return None
    major_version = int(match.group(1))
    minor_version = int(match.group(2))
    fix_version = int(match.group(3))
    fix_version_string = ''
    if fix_version:
      fix_version_string = '.%d' % fix_version
    return '%d.%d%s' % (major_version, minor_version, fix_version_string)


class BazelBuildBridge(object):
  """Handles invoking Bazel and unpacking generated binaries."""

  def __init__(self):
    self.verbose = True
    self.build_path = None

  def Run(self, args):
    """Executes a Bazel build based on the environment and given arguments."""
    xcode_action = os.environ['ACTION']

    # When invoked as an external build system script, Xcode will set ACTION to
    # an empty string.
    if not xcode_action:
      xcode_action = 'build'
    if xcode_action != 'build':
      sys.stderr.write('Xcode action is %s, ignoring.' % (xcode_action))
      return 0

    sdk_version = os.environ.get('SDK_VERSION', None)
    arch = os.environ.get('CURRENT_ARCH', None)
    main_group_path = os.getcwd()
    parser = _OptionsParser(sdk_version, arch, main_group_path)
    timer = Timer('Parsing options').Start()
    message, exit_code = parser.ParseOptions(args[1:])
    timer.End()
    if exit_code:
      self._PrintError('error: Option parsing failed: %s' % message)
      return exit_code

    self.verbose = parser.verbose
    self.bazel_bin_path = os.path.abspath('bazel-bin')

    (command, retval) = self._BuildBazelCommand(parser)
    if retval:
      return retval

    project_dir = os.environ['PROJECT_DIR']
    timer = Timer('Running Bazel').Start()
    exit_code = self._RunBazelAndPatchOutput(command,
                                             main_group_path,
                                             project_dir)
    timer.End()
    if exit_code:
      self._PrintError('Bazel build failed.')
      return exit_code

    exit_code = self._EnsureBazelBinSymlinkIsValid()
    if exit_code:
      self._PrintError('Failed to ensure existence of bazel-bin directory.')
      return exit_code

    if parser.install_generated_artifacts:
      bundle_output_path = os.environ['CODESIGNING_FOLDER_PATH']
      timer = Timer('Installing bundle artifacts').Start()
      exit_code = self._InstallBundleArtifact(bundle_output_path)
      timer.End()
      if exit_code:
        return exit_code

      timer = Timer('Installing DSYM bundles').Start()
      exit_code = self._InstallDSYMBundles(os.environ['BUILT_PRODUCTS_DIR'])
      timer.End()
      if exit_code:
        return exit_code

      # Starting with Xcode 7.3, XCTests inject several supporting frameworks
      # into the test host that need to be signed with the same identity as
      # the host itself.
      xcode_version = int(os.environ['XCODE_VERSION_MINOR'])
      platform_name = os.environ['PLATFORM_NAME']
      test_host_binary = os.environ.get('TEST_HOST', None)
      if (test_host_binary and xcode_version >= 730 and
          platform_name != 'iphonesimulator'):
        test_host_bundle = os.path.dirname(test_host_binary)
        timer = Timer('Re-signing injected test host artifacts').Start()
        exit_code = self._ResignTestHost(test_host_bundle)
        timer.End()
        if exit_code:
          return exit_code

    return 0

  def _BuildBazelCommand(self, options):
    """Builds up a commandline string suitable for running Bazel."""
    bazel_command = [options.bazel_executable]

    configuration = os.environ['CONFIGURATION']
    # Treat the special testrunner build config as a Debug compile.
    test_runner_config_prefix = '__TulsiTestRunner_'
    if configuration.startswith(test_runner_config_prefix):
      configuration = configuration[len(test_runner_config_prefix):]
    if configuration not in _OptionsParser.KNOWN_CONFIGS:
      print ('Warning: Unknown build configuration "%s", building in '
             'fastbuild mode' % configuration)
      configuration = 'Fastbuild'

    bazel_command.extend(options.GetStartupOptions(configuration))
    bazel_command.append('build')
    bazel_command.extend(options.GetBuildOptions(configuration))
    bazel_command.extend(options.targets)

    return (bazel_command, 0)

  def _RunBazelAndPatchOutput(self, command, main_group_path, project_dir):
    """Runs subprocess command, patching output as it's received."""
    self._PrintVerbose('Running "%s", patching output for main group path at '
                       '"%s" with project path at "%s".' % (' '.join(command),
                                                            main_group_path,
                                                            project_dir))
    patch_xcode_parsable_line = lambda x: x
    if main_group_path != project_dir:
      # Match (likely) filename:line_number: lines.
      xcode_parsable_line_regex = re.compile(r'([^:]+):\d+:')
      def PatchOutputLine(line):
        if xcode_parsable_line_regex.match(line):
          line = '%s/%s' % (main_group_path, line)
        return line
      patch_xcode_parsable_line = PatchOutputLine

    process = subprocess.Popen(command,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT,
                               bufsize=1)
    linebuf = ''
    while process.returncode is None:
      for line in process.stdout.readline():
        # Occasionally Popen's line-buffering appears to break down. Not
        # entirely certain why this happens, but we use an accumulator to
        # try to deal with it.
        if not line.endswith('\n'):
          linebuf += line
          continue
        line = patch_xcode_parsable_line(linebuf + line)
        linebuf = ''
        sys.stdout.write(line)
        sys.stdout.flush()
      process.poll()

    output, _ = process.communicate()
    output = linebuf + output
    for line in output.split('\n'):
      line = patch_xcode_parsable_line(line)
      print line

    return process.returncode

  def _EnsureBazelBinSymlinkIsValid(self):
    """Ensures that the bazel-bin symlink points at a real directory."""

    if not os.path.islink(self.bazel_bin_path):
      self._PrintVerbose('Warning: bazel-bin symlink at "%s" non-existent' %
                         (self.bazel_bin_path))
      return 0

    real_path = os.path.realpath(self.bazel_bin_path)
    if not os.path.isdir(real_path):
      try:
        os.makedirs(real_path)
      except OSError as e:
        self._PrintError('Failed to create bazel-bin at "%s". %s' %
                         (real_path, e))
        return 20
    return 0

  def _InstallBundleArtifact(self, output_path):
    """Installs Bazel-generated artifacts into the Xcode output directory."""
    if os.path.isdir(output_path):
      try:
        shutil.rmtree(output_path)
      except OSError as e:
        self._PrintError('Failed to remove stale output directory "%s". '
                         '%s' % (output_path, e))
        return 600

    self.build_path = os.path.join('bazel-bin',
                                   os.environ.get('BUILD_PATH', ''))

    bundle_artifact = os.environ['WRAPPER_NAME']
    full_bundle_artifact_path = os.path.join(self.build_path, bundle_artifact)
    if os.path.isdir(full_bundle_artifact_path):
      exit_code = self._CopyBundle(bundle_artifact,
                                   full_bundle_artifact_path,
                                   output_path)
      if exit_code:
        return exit_code
    else:
      ipa_artifact = os.environ['PRODUCT_NAME'] + '.ipa'
      exit_code = self._UnpackTarget(ipa_artifact, output_path)
      if exit_code:
        return exit_code
    return exit_code

  def _CopyBundle(self, source_path, full_source_path, output_path):
    """Copies the given bundle tothe given expected output path."""
    self._PrintVerbose('Copying %s to %s' % (source_path, output_path))
    try:
      shutil.copytree(full_source_path, output_path)
    except OSError as e:
      self._PrintError('Copy failed. %s' % e)
      return 650
    return 0

  def _UnpackTarget(self, ipa_artifact, output_path):
    """Unpacks generated IPA into the given expected output path."""
    self._PrintVerbose('Unpacking %s to %s' % (ipa_artifact, output_path))

    ipa_path = os.path.join(self.build_path, ipa_artifact)
    if not os.path.isfile(ipa_path):
      self._PrintError('Generated IPA not found at "%s"' % ipa_path)
      return 670

    # IPA file contents will be something like Payload/<app>.app/...
    # The base of the dirname within the Payload must match the last
    # component of output_path.
    expected_bundle_name = os.path.basename(output_path)
    expected_ipa_subpath = os.path.join('Payload', expected_bundle_name)

    with zipfile.ZipFile(ipa_path, 'r') as zf:
      for item in zf.infolist():
        filename = item.filename
        attributes = (item.external_attr >> 16) & 0777
        self._PrintVerbose('Extracting %s (%o)' % (filename, attributes))

        if len(filename) < len(expected_ipa_subpath):
          continue

        if not filename.startswith(expected_ipa_subpath):
          # TODO(abaire): Make an error if Bazel modifies this behavior.
          self._PrintVerbose('  Mismatched extraction path. IPA content at '
                             '"%s" expected to have subpath of "%s"' %
                             (filename, expected_ipa_subpath))

        dir_components = self._SplitPathComponents(filename)

        # Get the file's path, ignoring the payload components.
        subpath = os.path.join(*dir_components[2:])
        target_path = os.path.join(output_path, subpath)

        # Ensure the target directory exists.
        try:
          target_dir = os.path.dirname(target_path)
          if not os.path.isdir(target_dir):
            os.makedirs(target_dir)
        except OSError as e:
          self._PrintError(
              'Failed to create target path "%s" during extraction. %s' % (
                  target_path, e))
          return 671

        # If the archive item looks like a file, extract it.
        if not filename.endswith(os.sep):
          with zf.open(item) as src, file(target_path, 'wb') as dst:
            shutil.copyfileobj(src, dst)

        # Patch up the extracted file's attributes to match the zip content.
        if attributes:
          os.chmod(target_path, attributes)

    return 0

  def _InstallDSYMBundles(self, output_dir):
    """Copies any generated dSYM bundles to the given directory."""
    # TODO(abaire): Support mapping the dSYM generated for an obc_binary.
    # ios_application's will have a dSYM generated with the linked obj_binary's
    # filename, so the target_dsym will never actually match.
    target_dsym = os.environ.get('DWARF_DSYM_FILE_NAME', None)
    output_full_path = os.path.join(output_dir, target_dsym)
    if os.path.isdir(output_full_path):
      try:
        shutil.rmtree(output_full_path)
      except OSError as e:
        self._PrintError('Failed to remove stale output dSYM bundle ""%s". '
                         '%s' % (output_full_path, e))
        return 700

    input_dsym_full_path = os.path.join(self.build_path, target_dsym)
    if os.path.isdir(input_dsym_full_path):
      return self._CopyBundle(target_dsym,
                              input_dsym_full_path,
                              output_full_path)

    if 'BAZEL_BINARY_DSYM' in os.environ:
      # TODO(abaire): Remove this hack once Bazel generates dSYMs for
      #               ios_application/etc... bundles instead of their
      #               contained binaries.
      bazel_dsym_path = os.environ['BAZEL_BINARY_DSYM']
      build_path_prefix = os.environ.get('BUILD_PATH', '')
      if bazel_dsym_path.startswith(build_path_prefix):
        bazel_dsym_path = bazel_dsym_path[len(build_path_prefix) + 1:]
      input_dsym_full_path = os.path.join(self.build_path, bazel_dsym_path)
      if os.path.isdir(input_dsym_full_path):
        return self._CopyBundle(bazel_dsym_path,
                                input_dsym_full_path,
                                output_full_path)
    return 0

  def _ResignTestHost(self, test_host):
    """Re-signs the support frameworks in the given test host bundle."""
    signing_identity = self._ExtractSigningIdentity(test_host)
    if not signing_identity:
      return 800
    exit_code = self._ResignBundle(os.path.join(test_host,
                                                'Frameworks',
                                                'IDEBundleInjection.framework'),
                                   signing_identity)
    if exit_code != 0:
      return exit_code

    exit_code = self._ResignBundle(os.path.join(test_host,
                                                'Frameworks',
                                                'XCTest.framework'),
                                   signing_identity)
    if exit_code != 0:
      return exit_code
    # Note that Xcode 7.3 also re-signs the test_host itself, but this does
    # not appear to be necessary in the Bazel-backed case.
    return 0

  def _ResignBundle(self, bundle_path, signing_identity):
    """Re-signs the given path with a given signing identity."""
    command = ['xcrun',
               'codesign',
               '-f',
               '--preserve-metadata=identifier,entitlements',
               '--timestamp=none',
               '-s',
               signing_identity,
               bundle_path,
              ]
    process = subprocess.Popen(command,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    stdout, _ = process.communicate()
    if process.returncode:
      self._PrintError('Re-sign command %r failed. %s' % (command, stdout))
      return 800 + process.returncode
    return 0

  def _ExtractSigningIdentity(self, signed_bundle):
    """Returns the identity used to sign the given bundle path."""
    output = subprocess.check_output(['xcrun',
                                      'codesign',
                                      '-dvv',
                                      signed_bundle],
                                     stderr=subprocess.STDOUT)
    for line in output.split('\n'):
      if line.startswith('Authority='):
        return line[10:]
    self._PrintError('Failed to extract signing identity from %s' % output)
    return None

  def _SplitPathComponents(self, path):
    """Splits the given path into an array of all of its components."""
    components = path.split(os.sep)
    # Patch up the first component if path started with an os.sep
    if not components[0]:
      components[0] = os.sep
    return components

  def _PrintVerbose(self, msg):
    if self.verbose:
      sys.stdout.write(msg + '\n')
      sys.stdout.flush()

  def _PrintError(self, msg):
    sys.stderr.write(msg + '\n')
    sys.stderr.flush()


if __name__ == '__main__':
  _timer = Timer('Everything').Start()
  _exit_code = BazelBuildBridge().Run(sys.argv)
  _timer.End()
  sys.exit(_exit_code)
