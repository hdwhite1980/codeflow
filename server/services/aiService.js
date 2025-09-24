const { OpenAI } = require('openai');
const { Anthropic } = require('@anthropic-ai/sdk');
const { GoogleGenerativeAI } = require('@google/generative-ai');

class AIService {
  constructor() {
    // Initialize AI clients
    this.openai = process.env.OPENAI_API_KEY ? new OpenAI({
      apiKey: process.env.OPENAI_API_KEY
    }) : null;

    this.anthropic = process.env.ANTHROPIC_API_KEY ? new Anthropic({
      apiKey: process.env.ANTHROPIC_API_KEY
    }) : null;

    this.googleAI = process.env.GOOGLE_AI_API_KEY ? new GoogleGenerativeAI(
      process.env.GOOGLE_AI_API_KEY
    ) : null;

    this.supportedProviders = {
      openai: {
        models: ['gpt-4', 'gpt-4-turbo', 'gpt-3.5-turbo'],
        client: this.openai
      },
      anthropic: {
        models: ['claude-3-opus-20240229', 'claude-3-sonnet-20240229', 'claude-3-haiku-20240307'],
        client: this.anthropic
      },
      google: {
        models: ['gemini-pro', 'gemini-pro-vision'],
        client: this.googleAI
      }
    };
  }

  /**
   * Generate code using specified AI provider and model
   */
  async generateCode(provider, model, prompt, context = {}) {
    try {
      const providerConfig = this.supportedProviders[provider];
      
      if (!providerConfig || !providerConfig.client) {
        throw new Error(`AI provider ${provider} not configured or API key missing`);
      }

      if (!providerConfig.models.includes(model)) {
        throw new Error(`Model ${model} not supported for provider ${provider}`);
      }

      let response;
      
      switch (provider) {
        case 'openai':
          response = await this.generateWithOpenAI(model, prompt, context);
          break;
        case 'anthropic':
          response = await this.generateWithAnthropic(model, prompt, context);
          break;
        case 'google':
          response = await this.generateWithGoogle(model, prompt, context);
          break;
        default:
          throw new Error(`Unsupported provider: ${provider}`);
      }

      return {
        provider,
        model,
        prompt,
        response: response.content,
        usage: response.usage || {},
        context,
        timestamp: new Date().toISOString()
      };

    } catch (error) {
      console.error('AI generation error:', error);
      throw new Error(`AI generation failed: ${error.message}`);
    }
  }

  /**
   * Generate code with OpenAI
   */
  async generateWithOpenAI(model, prompt, context) {
    const systemMessage = this.buildSystemMessage(context);
    const userMessage = this.buildUserMessage(prompt, context);

    const completion = await this.openai.chat.completions.create({
      model: model,
      messages: [
        { role: 'system', content: systemMessage },
        { role: 'user', content: userMessage }
      ],
      max_tokens: context.maxTokens || 2000,
      temperature: context.temperature || 0.7,
      top_p: context.topP || 1,
      presence_penalty: context.presencePenalty || 0,
      frequency_penalty: context.frequencyPenalty || 0
    });

    return {
      content: completion.choices[0].message.content,
      usage: {
        promptTokens: completion.usage.prompt_tokens,
        completionTokens: completion.usage.completion_tokens,
        totalTokens: completion.usage.total_tokens
      },
      finishReason: completion.choices[0].finish_reason
    };
  }

  /**
   * Generate code with Anthropic Claude
   */
  async generateWithAnthropic(model, prompt, context) {
    const systemMessage = this.buildSystemMessage(context);
    const userMessage = this.buildUserMessage(prompt, context);

    const completion = await this.anthropic.messages.create({
      model: model,
      max_tokens: context.maxTokens || 2000,
      temperature: context.temperature || 0.7,
      system: systemMessage,
      messages: [
        { role: 'user', content: userMessage }
      ]
    });

    return {
      content: completion.content[0].text,
      usage: {
        promptTokens: completion.usage.input_tokens,
        completionTokens: completion.usage.output_tokens,
        totalTokens: completion.usage.input_tokens + completion.usage.output_tokens
      },
      finishReason: completion.stop_reason
    };
  }

  /**
   * Generate code with Google Gemini
   */
  async generateWithGoogle(model, prompt, context) {
    const genModel = this.googleAI.getGenerativeModel({ model: model });
    const systemMessage = this.buildSystemMessage(context);
    const userMessage = this.buildUserMessage(prompt, context);
    
    const fullPrompt = `${systemMessage}\n\nUser Request: ${userMessage}`;

    const result = await genModel.generateContent(fullPrompt);
    const response = await result.response;
    const content = response.text();

    return {
      content: content,
      usage: {
        // Google doesn't provide detailed token usage in basic tier
        promptTokens: 0,
        completionTokens: 0,
        totalTokens: 0
      },
      finishReason: 'stop'
    };
  }

  /**
   * Analyze code quality using AI
   */
  async analyzeCodeQuality(provider, model, code, language = 'javascript') {
    const prompt = `Please analyze this ${language} code for:
1. Code quality and best practices
2. Potential security vulnerabilities
3. Performance issues
4. Maintainability concerns
5. Suggestions for improvement

Code to analyze:
\`\`\`${language}
${code}
\`\`\`

Please provide a structured analysis with specific line numbers where applicable.`;

    const context = {
      intent: 'code_quality_analysis',
      constraints: ['focus_on_actionable_feedback', 'include_line_numbers', 'prioritize_security'],
      expectedOutput: 'structured_analysis'
    };

    return await this.generateCode(provider, model, prompt, context);
  }

  /**
   * Generate code improvement suggestions
   */
  async suggestImprovements(provider, model, code, issues, language = 'javascript') {
    const issuesText = issues.map(issue => `- ${issue.description} (Line ${issue.line})`).join('\n');
    
    const prompt = `Given this ${language} code and the identified issues, please provide improved code that addresses these problems:

Issues to fix:
${issuesText}

Original code:
\`\`\`${language}
${code}
\`\`\`

Please provide the improved code with explanations of the changes made.`;

    const context = {
      intent: 'code_improvement',
      constraints: ['maintain_functionality', 'improve_security', 'enhance_readability'],
      expectedOutput: 'improved_code_with_explanations'
    };

    return await this.generateCode(provider, model, prompt, context);
  }

  /**
   * Explain code functionality
   */
  async explainCode(provider, model, code, language = 'javascript') {
    const prompt = `Please explain what this ${language} code does:

\`\`\`${language}
${code}
\`\`\`

Provide:
1. High-level overview of functionality
2. Explanation of key functions and their purposes
3. Data flow and dependencies
4. Potential use cases
5. Any notable patterns or architectural decisions`;

    const context = {
      intent: 'code_explanation',
      constraints: ['clear_explanations', 'technical_accuracy', 'beginner_friendly'],
      expectedOutput: 'detailed_explanation'
    };

    return await this.generateCode(provider, model, prompt, context);
  }

  /**
   * Score AI response quality
   */
  scoreResponse(prompt, response, context, actualChanges = null) {
    let score = {
      followedInstructions: 5,
      codeQuality: 5,
      matchedIntent: 5,
      overall: 5
    };

    try {
      // Basic scoring heuristics
      
      // Check if response contains code when expected
      if (context.intent === 'code_generation' || context.intent === 'code_improvement') {
        const hasCodeBlocks = response.includes('```') || response.includes('function') || response.includes('const ') || response.includes('let ');
        if (!hasCodeBlocks) {
          score.followedInstructions -= 2;
        }
      }

      // Check response length appropriateness
      if (response.length < 50) {
        score.codeQuality -= 1;
      }

      // Check for explanations when requested
      if (context.constraints?.includes('include_explanations') && response.split('\n').length < 3) {
        score.followedInstructions -= 1;
      }

      // Intent matching
      const intentKeywords = {
        'code_generation': ['function', 'const', 'let', 'class', 'export'],
        'code_analysis': ['analysis', 'issue', 'problem', 'recommend'],
        'code_explanation': ['explain', 'does', 'purpose', 'function'],
        'code_improvement': ['improved', 'better', 'fix', 'enhanced']
      };

      const expectedKeywords = intentKeywords[context.intent] || [];
      const keywordMatches = expectedKeywords.filter(keyword => 
        response.toLowerCase().includes(keyword)
      ).length;

      if (keywordMatches === 0 && expectedKeywords.length > 0) {
        score.matchedIntent -= 2;
      } else if (keywordMatches < expectedKeywords.length / 2) {
        score.matchedIntent -= 1;
      }

      // Calculate overall score
      score.overall = Math.round(
        (score.followedInstructions + score.codeQuality + score.matchedIntent) / 3
      );

      // Ensure scores are within bounds
      Object.keys(score).forEach(key => {
        score[key] = Math.max(1, Math.min(5, score[key]));
      });

      return score;

    } catch (error) {
      console.error('Scoring error:', error);
      return score;
    }
  }

  // Helper methods
  buildSystemMessage(context) {
    let systemMessage = "You are a highly skilled software engineer and code reviewer. ";
    
    if (context.intent) {
      switch (context.intent) {
        case 'code_generation':
          systemMessage += "Generate clean, efficient, and well-documented code.";
          break;
        case 'code_analysis':
          systemMessage += "Provide thorough code analysis with actionable recommendations.";
          break;
        case 'code_explanation':
          systemMessage += "Explain code clearly for developers of all skill levels.";
          break;
        case 'code_improvement':
          systemMessage += "Suggest improvements while maintaining code functionality.";
          break;
      }
    }

    if (context.constraints && context.constraints.length > 0) {
      systemMessage += "\n\nConstraints: " + context.constraints.join(', ');
    }

    return systemMessage;
  }

  buildUserMessage(prompt, context) {
    let message = prompt;
    
    if (context.expectedChanges) {
      message += `\n\nExpected changes: ${context.expectedChanges}`;
    }

    return message;
  }

  /**
   * Get available providers and models
   */
  getAvailableProviders() {
    const available = {};
    
    Object.keys(this.supportedProviders).forEach(provider => {
      const config = this.supportedProviders[provider];
      available[provider] = {
        models: config.models,
        available: !!config.client
      };
    });

    return available;
  }
}

module.exports = new AIService();