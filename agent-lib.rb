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

  HARNESSES = %w[claude codex grok vibe opencode pi].freeze
  SPEC = %r{\A([a-z]+):([A-Za-z0-9][A-Za-z0-9._/\[\]-]*):([a-z]+)\z}

  CLAUDE_MODELS = %w[opus fable sonnet haiku].freeze
  CLAUDE_EFFORTS = %w[low medium high xhigh max].freeze
  PI_THINKING = %w[off minimal low medium high xhigh max].freeze

  # Vibe sets thinking per model in its config, and an opencode model without
  # variants has none to pick: their one effort is `default`.
  DEFAULT_EFFORT = %w[default].freeze

  CLEAR = { "pi" => "/new" }.freeze

  # The real host: listings time out rather than hang a start.
  class Host
    def installed?(command)
      ENV.fetch("PATH", "").split(File::PATH_SEPARATOR).any? { |dir| File.executable?(File.join(dir, command)) }
    end

    def capture(*command)
      out, = Open3.capture2("timeout", "20", *command, err: File::NULL)
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
    when "codex" then AgentCatalog.codex_models(host.capture("codex", "debug", "models"))
    when "grok" then AgentCatalog.grok_models(host.capture("grok", "models"))
    when "vibe" then AgentCatalog.vibe_models(host.read(AgentCatalog.vibe_config))
    when "opencode" then AgentCatalog.opencode_models(host.capture("opencode", "models", "--verbose"))
    when "pi" then AgentCatalog.pi_models(host.capture("pi", "--list-models"), host.read(AgentCatalog.pi_settings))
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
    when "vibe" then %W[env VIBE_ACTIVE_MODEL=#{model} vibe --yolo]
    when "opencode" then %W[opencode-variant #{model} #{effort}]
    when "pi" then %W[pi --model #{model} --thinking #{effort}]
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

  def vibe_config = File.join(ENV["VIBE_HOME"] || File.join(Dir.home, ".vibe"), "config.toml")

  # Its active model, then the aliases of its [[models]]. Vibe also routes
  # models its config does not name: those are reached by making one active.
  def vibe_models(toml)
    return [] unless toml

    active = toml[/^active_model\s*=\s*"([^"]+)"/, 1]
    aliases = toml.scan(/^alias\s*=\s*"([^"]+)"/).flatten
    ([active].compact + aliases).uniq.map { |model| [model, DEFAULT_EFFORT] }
  end

  # Its providers list embeddings, speech and years of older models too: only
  # those that think, call tools and write text are offered, so new ones show
  # up with no list to keep. Its efforts are the model's variants.
  def opencode_models(text)
    text.scan(/^(\S+\/\S+)\n(\{.*?^\})/m).filter_map do |model, json|
      row = JSON.parse(json)
      capabilities = row["capabilities"] || {}
      next unless capabilities["reasoning"] && capabilities["toolcall"] && capabilities.dig("output", "text")

      variants = (row["variants"] || {}).keys
      [model, variants.empty? ? DEFAULT_EFFORT : variants]
    rescue JSON::ParserError
      nil
    end
  end

  def opencode_state = File.join(ENV["XDG_STATE_HOME"] || File.join(Dir.home, ".local", "state"), "opencode", "model.json")

  # opencode's TUI takes no variant: it starts on the one it remembers per
  # model, in its state, which also wins over an agent's configured variant.
  # `default` forgets it, as the TUI does.
  def remember_opencode_variant(state, model, variant)
    data = File.file?(state) ? JSON.parse(File.read(state)) : {}
    variants = data["variant"] ||= {}
    variant == "default" ? variants.delete(model) : variants[model] = variant
    FileUtils.mkdir_p(File.dirname(state))
    File.write("#{state}.new", JSON.generate(data))
    File.rename("#{state}.new", state)
  end

  def pi_settings = File.join(ENV["PI_CODING_AGENT_DIR"] || File.join(Dir.home, ".pi", "agent"), "settings.json")

  # Its scoped models (enabledModels, what its Ctrl+P cycles through), in their
  # order, names or globs; without any, the models that think, as for opencode.
  def pi_models(table, settings = nil)
    thinks = table.lines.drop(1).filter_map do |line|
      provider, model, _context, _max_out, thinking = line.split
      ["#{provider}/#{model}", thinking == "yes"] if thinking
    end.to_h
    scoped = (JSON.parse(settings || "{}")["enabledModels"] rescue nil) || []
    models = if scoped.empty?
               thinks.select { |_model, thinking| thinking }.keys
             else
               scoped.flat_map { |pattern| thinks.keys.select { |model| File.fnmatch(pattern, model) } }.uniq
             end
    models.map { |model| [model, thinks[model] ? PI_THINKING : %w[off]] }
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
