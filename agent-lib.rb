# Coding agents, for `agent-catalog` and `agent-pick`.
#
# An agent is a spec, `harness:model:effort` (claude:opus:high,
# pi:hyper/glm-5.3:high), with the slash command that clears its context and
# the command line that starts it with no permission prompt: run it in a
# sandbox. This file is the only one that knows a harness's flags.

require "fileutils"
require "json"
require "open3"
require "shellwords"

module AgentCatalog
  module_function

  # Neither vibe (herdr detects no vibe agent, none in 0.9.3: `herdr agent
  # prompt` and `wait` cannot reach one) nor opencode's full TUI. The TUI is
  # fine interactively, and no less safe in a sandbox than mini — same binary,
  # same mounts — but it cannot be started at a pinned model and effort, which
  # is this catalog's contract:
  #   - it takes no --model or --variant flag (anomalyco/opencode#7354, closed
  #     unfixed, PR #7358 unmerged);
  #   - a variant configured for an agent does not reach it either
  #     (anomalyco/opencode#7138, then #22065, closed not planned; PR #7156);
  #   - it starts on the variant it remembers per model, in model.json — the
  #     host's copy shared by its own sessions, and a sandbox's copy empty, so
  #     a fresh sandbox session would run the model default, not the picked
  #     effort.
  # opencode therefore runs as `opencode mini`, whose --model takes
  # provider/model#variant (verified on v2.1.3): the flag pins the effort. An
  # unknown #variant falls back to the model default silently, so the variant
  # lists here come from the model catalog. Switch this entry to the TUI when
  # its root command grows a startup model/variant flag — until then, mini
  # only.
  HARNESSES = %w[claude codex grok opencode opencode-free pi].freeze
  SPEC = %r{\A([a-z][a-z-]*):([A-Za-z0-9][A-Za-z0-9._/\[\]-]*):([a-z]+)\z}

  CLAUDE_MODELS = %w[opus fable sonnet haiku].freeze
  CLAUDE_EFFORTS = %w[low medium high xhigh max].freeze
  PI_THINKING = %w[off minimal low medium high xhigh max].freeze

  CLEAR = { "opencode" => "/new", "opencode-free" => "/new", "pi" => "/new" }.freeze

  # opencode on the config home of the user's `opencode-free` wrapper: the free
  # models, without the zero-data-retention policies of the default config.
  FREE_CONFIG_HOME = File.join(Dir.home, ".config-free")

  # The real host: listings time out rather than hang a start.
  class Host
    def installed?(command)
      ENV.fetch("PATH", "").split(File::PATH_SEPARATOR).any? { |dir| File.executable?(File.join(dir, command)) }
    end

    def capture(*command, input: nil)
      out, = Open3.capture2("timeout", "20", *command, stdin_data: input.to_s, err: File::NULL)
      out
    rescue SystemCallError
      ""
    end

    def read(path) = File.file?(path) ? File.read(path) : nil
  end

  # One `<spec><TAB><clear><TAB><command line>` per agent, the harnesses
  # listed in parallel.
  def lines(host = Host.new)
    HARNESSES.select { |harness| host.installed?(harness) }
             .map { |harness| Thread.new { AgentCatalog.agents(harness, host) } }
             .flat_map(&:value)
  end

  def agents(harness, host)
    AgentCatalog.models(harness, host).flat_map do |model, efforts|
      efforts.filter_map do |effort|
        spec = "#{harness}:#{model}:#{effort}"
        next unless SPEC.match?(spec)

        [spec, CLEAR.fetch(harness, "/clear"), AgentCatalog.shell_line(AgentCatalog.command(harness, model, effort))]
          .join("\t")
      end
    end
  end

  # Quotes only the words a shell would change, so the line stays readable.
  def shell_line(words)
    words.map { |word| word.match?(%r{\A[A-Za-z0-9_./=:@%+,-]+\z}) ? word : Shellwords.escape(word) }.join(" ")
  end

  # [[model, efforts]] the harness offers now.
  def models(harness, host)
    case harness
    when "claude" then CLAUDE_MODELS.map { |model| [model, CLAUDE_EFFORTS] }
    when "opencode" then AgentCatalog.opencode_models(host.capture("opencode", "api", "get", "/api/model"))
    when "opencode-free" then AgentCatalog.opencode_models(host.capture("opencode-free", "api", "get", "/api/model"))
    when "codex" then AgentCatalog.codex_models(host.capture("codex", "debug", "models"))
    when "grok" then AgentCatalog.grok_models(host.capture("grok", "models"))
    when "pi"
      # Its RPC mode, unlike --list-models, tells each model's thinking levels.
      rpc = host.capture("pi", "--mode", "rpc", "--no-session", input: %({"type":"get_available_models"}\n))
      AgentCatalog.pi_models(rpc, host.read(AgentCatalog.pi_settings))
    end
  end

  def command(harness, model, effort)
    case harness
    when "claude"
      # Prompt suggestions render where the agent's output would.
      %W[env CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false claude --dangerously-skip-permissions
         --model #{model} --effort #{effort}]
    when "codex"
      # Its startup update check fails in a sandbox, whose npm prefix is
      # read-only, and takes the agent down with it.
      %W[codex --dangerously-bypass-approvals-and-sandbox -m #{model} -c model_reasoning_effort=#{effort}
         -c check_for_update_on_startup=false]
    when "grok" then %W[grok --yolo -m #{model} --reasoning-effort #{effort}]
    when "pi" then %W[pi --model #{model} --thinking #{effort}]
    when "opencode"
      # mini, not the TUI: only mini takes the #variant (see the header). The
      # inline CLI config auto-accepts permissions — the sandbox is the
      # boundary, as for the other harnesses.
      %W[env OPENCODE_CLI_CONFIG_CONTENT={"session":{"permissions":"autoaccept"}} opencode mini --standalone
         --model #{model}##{effort}]
    when "opencode-free"
      # The wrapper's config home, but not its OPENCODE_DB: that path is
      # read-only in a sandbox, whose own opencode data dir is the writable one.
      %W[env XDG_CONFIG_HOME=#{FREE_CONFIG_HOME}
         OPENCODE_CLI_CONFIG_CONTENT={"session":{"permissions":"autoaccept"}} opencode mini --standalone
         --model #{model}##{effort}]
    end
  end

  # The models codex's own picker shows.
  def codex_models(json)
    rows = JSON.parse(json)
    rows = rows.is_a?(Hash) ? (rows["models"] || rows["data"] || []) : rows
    rows.reject { |row| row["visibility"] == "hide" }.map do |row|
      levels = row["supported_reasoning_levels"] || row["supported_reasoning_efforts"] || []
      [row["slug"] || row["id"], levels.map { |level| level.is_a?(Hash) ? level["effort"] : level }]
    end
  rescue JSON::ParserError
    []
  end

  # grok lists no efforts: these are the ones grok accepts for each model.
  def grok_models(text)
    text.scan(/^\s*[*-]\s+(\S+)/).flatten.map do |model|
      [model, model == "grok-4.5" ? %w[low medium high] : %w[low medium high xhigh]]
    end
  end

  # /api/model lists every model with its variant ids, which are the efforts.
  def opencode_models(json)
    rows = JSON.parse(json)
    rows = rows.is_a?(Hash) ? (rows["models"] || rows["data"] || []) : rows
    rows.filter_map do |row|
      efforts = (row["variants"] || []).map { |v| v.is_a?(Hash) ? v["id"] : v }.compact.uniq
      ["#{row['providerID']}/#{row['id']}", efforts] unless efforts.empty?
    end
  rescue JSON::ParserError
    []
  end

  def pi_settings = File.join(ENV["PI_CODING_AGENT_DIR"] || File.join(Dir.home, ".pi", "agent"), "settings.json")

  # Its scoped models (enabledModels, what its Ctrl+P cycles through), in their
  # order, names or globs; without any, the models that think.
  def pi_models(rpc, settings = nil)
    levels = AgentCatalog.pi_available_models(rpc).to_h do |row|
      ["#{row["provider"]}/#{row["id"]}", AgentCatalog.pi_thinking_levels(row)]
    end
    scoped = (JSON.parse(settings || "{}")["enabledModels"] rescue nil) || []
    models = if scoped.empty?
               levels.reject { |_model, model_levels| model_levels == %w[off] }.keys
             else
               scoped.flat_map { |pattern| levels.keys.select { |model| File.fnmatch(pattern, model) } }.uniq
             end
    models.map { |model| [model, levels[model]] }
  end

  # The models of its get_available_models answer, among the other messages
  # its RPC mode prints.
  def pi_available_models(rpc)
    rpc.lines.each do |line|
      message = JSON.parse(line) rescue next
      return message.dig("data", "models") || [] if message["command"] == "get_available_models"
    end
    []
  end

  # As pi's getSupportedThinkingLevels: a level its thinkingLevelMap sets to
  # null is out, xhigh and max only when the map names them.
  def pi_thinking_levels(row)
    return %w[off] unless row["reasoning"]

    map = row["thinkingLevelMap"] || {}
    PI_THINKING.select do |level|
      next false if map.key?(level) && map[level].nil?

      %w[xhigh max].include?(level) ? map.key?(level) : true
    end
  end
end

# The agent for each of a caller's roles (any names: tab1, reviewer...), picked
# with fzf among the catalog's, harness then model then effort. The picks are
# remembered as `role=spec` lines, kept with one Enter at the next start.
class AgentPick
  class Failed < StandardError; end

  ROLE = /\A[a-z0-9][a-z0-9_-]*\z/

  # fzf: ->(header, options) { the option chosen, or nil }
  def initialize(catalog:, choices:, fzf: AgentPick.method(:fzf), input: $stdin, err: $stderr, tty: $stdin.tty?)
    @catalog = catalog.to_h { |line| [line.split("\t", 2).first, line] }
    @choices = choices
    @fzf = fzf
    @input = input
    @err = err
    @tty = tty
  end

  # One `<role><TAB><catalog line>` per role.
  def lines(roles)
    raise Failed, "no role given" if roles.empty?

    roles.each { |role| raise Failed, "bad role (lowercase letters, digits, - and _): #{role.inspect}" unless ROLE.match?(role) }
    agents = roles.to_h { |role| [role, remembered(role)] }
    unpicked = agents.find { |_role, spec| !@catalog.key?(spec) }&.first
    raise Failed, "no terminal to pick the #{unpicked} agent" if unpicked && !@tty

    agents = pick_all(agents) unless !unpicked && keep?(agents)
    agents.map { |role, spec| "#{role}\t#{@catalog.fetch(spec)}" }
  end

  def self.fzf(header, options)
    out, status = Open3.capture2("fzf", "--height=40%", "--reverse", "--header=#{header}",
                                 stdin_data: options.join("\n"))
    status.success? ? out.strip : nil
  end

  private

  # A closed input keeps them: a start nobody answers must not block.
  def keep?(agents)
    return true unless @tty

    width = agents.keys.map(&:length).max
    @err.puts "Keep these agents?"
    agents.each { |role, spec| @err.puts "  #{role.ljust(width)}  #{spec}" }
    @err.print "[Y/n] "
    !%w[n no].include?(@input.gets.to_s.strip.downcase)
  end

  def pick_all(agents)
    agents.to_h do |role, spec|
      picked = pick(role, spec.to_s)
      remember(role, picked)
      [role, picked]
    end
  end

  def pick(role, current)
    current_harness, current_model, current_effort = current.split(":")
    specs = @catalog.keys.map { |spec| spec.split(":") }
    harness = choose("#{role}: harness", specs.map(&:first), current_harness)
    specs = specs.select { |spec| spec[0] == harness }
    model = choose("#{role}: #{harness} model", specs.map { |spec| spec[1] }, current_model)
    effort = choose("#{role}: effort", specs.select { |spec| spec[1] == model }.map(&:last), current_effort)
    [harness, model, effort].join(":")
  end

  def choose(header, options, default)
    options = options.uniq
    options = ([default] & options) + (options - [default])
    @fzf.call(header, options) || raise(Failed, "nothing picked")
  end

  def remembered(role)
    return nil unless File.file?(@choices)

    File.readlines(@choices, chomp: true).find { |line| line.start_with?("#{role}=") }&.split("=", 2)&.last
  end

  def remember(role, spec)
    kept = File.file?(@choices) ? File.readlines(@choices, chomp: true).reject { |line| line.start_with?("#{role}=") } : []
    FileUtils.mkdir_p(File.dirname(@choices))
    File.write("#{@choices}.new", (kept + ["#{role}=#{spec}"]).join("\n") + "\n")
    File.rename("#{@choices}.new", @choices)
  end
end
