const express = require('express');
const AISession = require('../models/AISession');
const aiService = require('../services/aiService');
const Project = require('../models/Project');
const CodeFile = require('../models/CodeFile');

const router = express.Router();

/**
 * GET /api/ai/providers
 * Get available AI providers and models
 */
router.get('/providers', (req, res) => {
  try {
    const providers = aiService.getAvailableProviders();
    
    res.json({
      success: true,
      providers: providers
    });

  } catch (error) {
    console.error('Get providers error:', error);
    res.status(500).json({ error: 'Failed to retrieve AI providers' });
  }
});

/**
 * POST /api/ai/generate
 * Generate code using AI
 */
router.post('/generate', async (req, res) => {
  try {
    const { 
      provider, 
      model, 
      prompt, 
      projectId,
      context = {} 
    } = req.body;

    if (!provider || !model || !prompt) {
      return res.status(400).json({ 
        error: 'Provider, model, and prompt are required' 
      });
    }

    // Generate code using AI service
    const result = await aiService.generateCode(provider, model, prompt, context);

    // Save AI session if projectId provided
    let session = null;
    if (projectId) {
      session = new AISession({
        userId: req.userId || 'anonymous',
        projectId: projectId,
        provider: provider,
        model: model,
        prompt: prompt,
        response: result.response,
        context: context,
        codeGenerated: result.response,
        score: aiService.scoreResponse(prompt, result.response, context)
      });

      await session.save();
    }

    res.json({
      success: true,
      result: result,
      sessionId: session ? session._id : null
    });

  } catch (error) {
    console.error('AI generation error:', error);
    res.status(500).json({ 
      error: 'Failed to generate code', 
      details: error.message 
    });
  }
});

/**
 * POST /api/ai/analyze-quality
 * Analyze code quality using AI
 */
router.post('/analyze-quality', async (req, res) => {
  try {
    const { 
      provider, 
      model, 
      code, 
      language = 'javascript',
      projectId 
    } = req.body;

    if (!provider || !model || !code) {
      return res.status(400).json({ 
        error: 'Provider, model, and code are required' 
      });
    }

    // Analyze code quality using AI
    const result = await aiService.analyzeCodeQuality(provider, model, code, language);

    // Save AI session if projectId provided
    let session = null;
    if (projectId) {
      session = new AISession({
        userId: req.userId || 'anonymous',
        projectId: projectId,
        provider: provider,
        model: model,
        prompt: `Analyze code quality for ${language} code`,
        response: result.response,
        context: {
          intent: 'code_quality_analysis',
          language: language
        },
        score: aiService.scoreResponse(
          `Analyze code quality for ${language} code`, 
          result.response, 
          { intent: 'code_quality_analysis' }
        )
      });

      await session.save();
    }

    res.json({
      success: true,
      analysis: result,
      sessionId: session ? session._id : null
    });

  } catch (error) {
    console.error('AI quality analysis error:', error);
    res.status(500).json({ 
      error: 'Failed to analyze code quality', 
      details: error.message 
    });
  }
});

/**
 * POST /api/ai/suggest-improvements
 * Get improvement suggestions using AI
 */
router.post('/suggest-improvements', async (req, res) => {
  try {
    const { 
      provider, 
      model, 
      code, 
      issues = [],
      language = 'javascript',
      projectId 
    } = req.body;

    if (!provider || !model || !code) {
      return res.status(400).json({ 
        error: 'Provider, model, and code are required' 
      });
    }

    // Get improvement suggestions using AI
    const result = await aiService.suggestImprovements(provider, model, code, issues, language);

    // Save AI session if projectId provided
    let session = null;
    if (projectId) {
      session = new AISession({
        userId: req.userId || 'anonymous',
        projectId: projectId,
        provider: provider,
        model: model,
        prompt: `Suggest improvements for ${language} code`,
        response: result.response,
        context: {
          intent: 'code_improvement',
          language: language,
          issues: issues
        },
        score: aiService.scoreResponse(
          `Suggest improvements for ${language} code`, 
          result.response, 
          { intent: 'code_improvement' }
        )
      });

      await session.save();
    }

    res.json({
      success: true,
      suggestions: result,
      sessionId: session ? session._id : null
    });

  } catch (error) {
    console.error('AI improvement suggestions error:', error);
    res.status(500).json({ 
      error: 'Failed to generate improvement suggestions', 
      details: error.message 
    });
  }
});

/**
 * POST /api/ai/explain-code
 * Get code explanation using AI
 */
router.post('/explain-code', async (req, res) => {
  try {
    const { 
      provider, 
      model, 
      code, 
      language = 'javascript',
      projectId 
    } = req.body;

    if (!provider || !model || !code) {
      return res.status(400).json({ 
        error: 'Provider, model, and code are required' 
      });
    }

    // Get code explanation using AI
    const result = await aiService.explainCode(provider, model, code, language);

    // Save AI session if projectId provided
    let session = null;
    if (projectId) {
      session = new AISession({
        userId: req.userId || 'anonymous',
        projectId: projectId,
        provider: provider,
        model: model,
        prompt: `Explain ${language} code functionality`,
        response: result.response,
        context: {
          intent: 'code_explanation',
          language: language
        },
        score: aiService.scoreResponse(
          `Explain ${language} code functionality`, 
          result.response, 
          { intent: 'code_explanation' }
        )
      });

      await session.save();
    }

    res.json({
      success: true,
      explanation: result,
      sessionId: session ? session._id : null
    });

  } catch (error) {
    console.error('AI code explanation error:', error);
    res.status(500).json({ 
      error: 'Failed to explain code', 
      details: error.message 
    });
  }
});

/**
 * GET /api/ai/sessions/:projectId
 * Get AI sessions for a project
 */
router.get('/sessions/:projectId', async (req, res) => {
  try {
    const { projectId } = req.params;
    const { limit = 10, skip = 0 } = req.query;

    const sessions = await AISession.find({ projectId })
      .sort({ createdAt: -1 })
      .limit(parseInt(limit))
      .skip(parseInt(skip))
      .select('-response'); // Exclude long response text for list view

    const totalSessions = await AISession.countDocuments({ projectId });

    res.json({
      success: true,
      sessions: sessions,
      pagination: {
        total: totalSessions,
        limit: parseInt(limit),
        skip: parseInt(skip),
        hasMore: totalSessions > (parseInt(skip) + sessions.length)
      }
    });

  } catch (error) {
    console.error('Get AI sessions error:', error);
    res.status(500).json({ error: 'Failed to retrieve AI sessions' });
  }
});

/**
 * GET /api/ai/session/:sessionId
 * Get specific AI session details
 */
router.get('/session/:sessionId', async (req, res) => {
  try {
    const { sessionId } = req.params;

    const session = await AISession.findById(sessionId)
      .populate('projectId', 'name')
      .populate('userId', 'name email');

    if (!session) {
      return res.status(404).json({ error: 'AI session not found' });
    }

    res.json({
      success: true,
      session: session
    });

  } catch (error) {
    console.error('Get AI session error:', error);
    res.status(500).json({ error: 'Failed to retrieve AI session' });
  }
});

/**
 * PUT /api/ai/session/:sessionId/score
 * Update AI session score manually
 */
router.put('/session/:sessionId/score', async (req, res) => {
  try {
    const { sessionId } = req.params;
    const { score } = req.body;

    if (!score || typeof score !== 'object') {
      return res.status(400).json({ 
        error: 'Valid score object is required' 
      });
    }

    const session = await AISession.findByIdAndUpdate(
      sessionId, 
      { score: score },
      { new: true }
    );

    if (!session) {
      return res.status(404).json({ error: 'AI session not found' });
    }

    res.json({
      success: true,
      session: session
    });

  } catch (error) {
    console.error('Update session score error:', error);
    res.status(500).json({ error: 'Failed to update session score' });
  }
});

/**
 * GET /api/ai/stats/:projectId
 * Get AI usage statistics for a project
 */
router.get('/stats/:projectId', async (req, res) => {
  try {
    const { projectId } = req.params;

    // Get session statistics
    const totalSessions = await AISession.countDocuments({ projectId });
    const sessionsByProvider = await AISession.aggregate([
      { $match: { projectId: require('mongoose').Types.ObjectId(projectId) } },
      { $group: { _id: '$provider', count: { $sum: 1 } } }
    ]);

    const avgScores = await AISession.aggregate([
      { $match: { projectId: require('mongoose').Types.ObjectId(projectId) } },
      { 
        $group: { 
          _id: null, 
          avgFollowedInstructions: { $avg: '$score.followedInstructions' },
          avgCodeQuality: { $avg: '$score.codeQuality' },
          avgMatchedIntent: { $avg: '$score.matchedIntent' },
          avgOverall: { $avg: '$score.overall' }
        } 
      }
    ]);

    const recentSessions = await AISession.find({ projectId })
      .sort({ createdAt: -1 })
      .limit(5)
      .select('provider model context.intent createdAt score.overall');

    res.json({
      success: true,
      stats: {
        totalSessions,
        sessionsByProvider: sessionsByProvider.reduce((acc, item) => {
          acc[item._id] = item.count;
          return acc;
        }, {}),
        averageScores: avgScores[0] || {
          avgFollowedInstructions: 0,
          avgCodeQuality: 0,
          avgMatchedIntent: 0,
          avgOverall: 0
        },
        recentSessions
      }
    });

  } catch (error) {
    console.error('Get AI stats error:', error);
    res.status(500).json({ error: 'Failed to retrieve AI statistics' });
  }
});

module.exports = router;