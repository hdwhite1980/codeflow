const express = require('express');
const Project = require('../models/Project');
const CodeFile = require('../models/CodeFile');

const router = express.Router();

/**
 * GET /api/projects
 * Get all projects for user
 */
router.get('/', async (req, res) => {
  try {
    const projects = await Project.find({ 
      userId: req.userId || 'anonymous' 
    })
    .populate('files', 'originalName language size createdAt')
    .sort({ updatedAt: -1 });

    const projectsWithStats = await Promise.all(projects.map(async (project) => {
      const fileCount = project.files.length;
      const totalSize = project.files.reduce((sum, file) => sum + (file.size || 0), 0);
      
      // Get latest analysis stats
      const files = await CodeFile.find({ projectId: project._id });
      const avgComplexity = files.length > 0 ? 
        files.reduce((sum, file) => sum + (file.analysis?.complexity?.cyclomatic || 0), 0) / files.length : 0;
      const avgSecurity = files.length > 0 ?
        files.reduce((sum, file) => sum + (file.analysis?.security?.score || 5), 0) / files.length : 5;

      return {
        ...project.toObject(),
        stats: {
          fileCount,
          totalSize,
          avgComplexity: Math.round(avgComplexity * 10) / 10,
          avgSecurity: Math.round(avgSecurity * 10) / 10
        }
      };
    }));

    res.json({
      success: true,
      projects: projectsWithStats
    });

  } catch (error) {
    console.error('Get projects error:', error);
    res.status(500).json({ error: 'Failed to retrieve projects' });
  }
});

/**
 * POST /api/projects
 * Create new project
 */
router.post('/', async (req, res) => {
  try {
    const { name, description, tags } = req.body;

    if (!name || !name.trim()) {
      return res.status(400).json({ error: 'Project name is required' });
    }

    const project = new Project({
      name: name.trim(),
      description: description || '',
      userId: req.userId || 'anonymous',
      tags: tags || [],
      files: []
    });

    await project.save();

    res.json({
      success: true,
      project: project
    });

  } catch (error) {
    console.error('Create project error:', error);
    res.status(500).json({ error: 'Failed to create project' });
  }
});

/**
 * GET /api/projects/:projectId
 * Get specific project with files
 */
router.get('/:projectId', async (req, res) => {
  try {
    const { projectId } = req.params;
    
    const project = await Project.findById(projectId)
      .populate({
        path: 'files',
        select: 'originalName language size createdAt analysis.complexity.cyclomatic analysis.security.score'
      });

    if (!project) {
      return res.status(404).json({ error: 'Project not found' });
    }

    // Calculate project statistics
    const stats = {
      fileCount: project.files.length,
      totalSize: project.files.reduce((sum, file) => sum + (file.size || 0), 0),
      avgComplexity: project.files.length > 0 ? 
        project.files.reduce((sum, file) => sum + (file.analysis?.complexity?.cyclomatic || 0), 0) / project.files.length : 0,
      avgSecurity: project.files.length > 0 ?
        project.files.reduce((sum, file) => sum + (file.analysis?.security?.score || 5), 0) / project.files.length : 5,
      languages: [...new Set(project.files.map(f => f.language))],
      lastModified: project.files.length > 0 ? 
        Math.max(...project.files.map(f => new Date(f.createdAt).getTime())) : project.createdAt
    };

    res.json({
      success: true,
      project: {
        ...project.toObject(),
        stats
      }
    });

  } catch (error) {
    console.error('Get project error:', error);
    res.status(500).json({ error: 'Failed to retrieve project' });
  }
});

/**
 * PUT /api/projects/:projectId
 * Update project
 */
router.put('/:projectId', async (req, res) => {
  try {
    const { projectId } = req.params;
    const { name, description, tags, settings } = req.body;

    const project = await Project.findById(projectId);
    
    if (!project) {
      return res.status(404).json({ error: 'Project not found' });
    }

    // Update fields
    if (name) project.name = name.trim();
    if (description !== undefined) project.description = description;
    if (tags) project.tags = tags;
    if (settings) project.settings = { ...project.settings, ...settings };

    await project.save();

    res.json({
      success: true,
      project: project
    });

  } catch (error) {
    console.error('Update project error:', error);
    res.status(500).json({ error: 'Failed to update project' });
  }
});

/**
 * DELETE /api/projects/:projectId
 * Delete project and all associated files
 */
router.delete('/:projectId', async (req, res) => {
  try {
    const { projectId } = req.params;

    const project = await Project.findById(projectId);
    
    if (!project) {
      return res.status(404).json({ error: 'Project not found' });
    }

    // Delete all associated files
    await CodeFile.deleteMany({ projectId: projectId });

    // Delete project
    await Project.findByIdAndDelete(projectId);

    res.json({
      success: true,
      message: 'Project deleted successfully'
    });

  } catch (error) {
    console.error('Delete project error:', error);
    res.status(500).json({ error: 'Failed to delete project' });
  }
});

/**
 * GET /api/projects/:projectId/files
 * Get all files in project with detailed analysis
 */
router.get('/:projectId/files', async (req, res) => {
  try {
    const { projectId } = req.params;
    
    const files = await CodeFile.find({ projectId })
      .select('-content') // Exclude file content for performance
      .sort({ createdAt: -1 });

    res.json({
      success: true,
      files: files
    });

  } catch (error) {
    console.error('Get project files error:', error);
    res.status(500).json({ error: 'Failed to retrieve project files' });
  }
});

/**
 * GET /api/projects/:projectId/dashboard
 * Get dashboard data for project
 */
router.get('/:projectId/dashboard', async (req, res) => {
  try {
    const { projectId } = req.params;
    
    const project = await Project.findById(projectId);
    const files = await CodeFile.find({ projectId });

    if (!project) {
      return res.status(404).json({ error: 'Project not found' });
    }

    // Calculate comprehensive dashboard data
    const dashboard = {
      project: {
        name: project.name,
        description: project.description,
        createdAt: project.createdAt,
        updatedAt: project.updatedAt
      },
      overview: {
        totalFiles: files.length,
        totalSize: files.reduce((sum, file) => sum + (file.size || 0), 0),
        languages: getLanguageBreakdown(files),
        avgComplexity: calculateAverageComplexity(files),
        avgSecurity: calculateAverageSecurity(files),
        totalFunctions: files.reduce((sum, file) => 
          sum + (file.analysis?.dependencies?.functions?.length || 0), 0),
        totalVariables: files.reduce((sum, file) => 
          sum + (file.analysis?.dependencies?.variables?.length || 0), 0)
      },
      complexity: {
        distribution: getComplexityDistribution(files),
        topComplexFiles: getTopComplexFiles(files, 5),
        complexityTrend: getComplexityTrend(files)
      },
      security: {
        overallScore: calculateAverageSecurity(files),
        issueCount: getTotalSecurityIssues(files),
        issuesByType: getSecurityIssuesByType(files),
        riskFiles: getRiskFiles(files, 5)
      },
      dependencies: {
        totalImports: getTotalImports(files),
        externalDependencies: getExternalDependencies(files),
        internalDependencies: getInternalDependencies(files),
        cloudServices: getCloudServiceUsage(files)
      },
      recentActivity: getRecentActivity(files, 10)
    };

    res.json({
      success: true,
      dashboard: dashboard
    });

  } catch (error) {
    console.error('Get project dashboard error:', error);
    res.status(500).json({ error: 'Failed to retrieve project dashboard' });
  }
});

// Helper functions for dashboard calculations
function getLanguageBreakdown(files) {
  const languages = {};
  files.forEach(file => {
    languages[file.language] = (languages[file.language] || 0) + 1;
  });
  return languages;
}

function calculateAverageComplexity(files) {
  if (files.length === 0) return 0;
  const total = files.reduce((sum, file) => 
    sum + (file.analysis?.complexity?.cyclomatic || 0), 0);
  return Math.round((total / files.length) * 10) / 10;
}

function calculateAverageSecurity(files) {
  if (files.length === 0) return 5;
  const total = files.reduce((sum, file) => 
    sum + (file.analysis?.security?.score || 5), 0);
  return Math.round((total / files.length) * 10) / 10;
}

function getComplexityDistribution(files) {
  const distribution = { low: 0, medium: 0, high: 0, critical: 0 };
  
  files.forEach(file => {
    const complexity = file.analysis?.complexity?.cyclomatic || 0;
    if (complexity <= 5) distribution.low++;
    else if (complexity <= 10) distribution.medium++;
    else if (complexity <= 20) distribution.high++;
    else distribution.critical++;
  });
  
  return distribution;
}

function getTopComplexFiles(files, limit = 5) {
  return files
    .filter(file => file.analysis?.complexity?.cyclomatic)
    .sort((a, b) => b.analysis.complexity.cyclomatic - a.analysis.complexity.cyclomatic)
    .slice(0, limit)
    .map(file => ({
      name: file.originalName,
      complexity: file.analysis.complexity.cyclomatic,
      id: file._id
    }));
}

function getTotalSecurityIssues(files) {
  return files.reduce((sum, file) => 
    sum + (file.analysis?.security?.issues?.length || 0), 0);
}

function getSecurityIssuesByType(files) {
  const issueTypes = {};
  
  files.forEach(file => {
    if (file.analysis?.security?.issues) {
      file.analysis.security.issues.forEach(issue => {
        issueTypes[issue.type] = (issueTypes[issue.type] || 0) + 1;
      });
    }
  });
  
  return issueTypes;
}

function getRiskFiles(files, limit = 5) {
  return files
    .filter(file => file.analysis?.security?.score < 4)
    .sort((a, b) => a.analysis.security.score - b.analysis.security.score)
    .slice(0, limit)
    .map(file => ({
      name: file.originalName,
      securityScore: file.analysis.security.score,
      issueCount: file.analysis.security.issues?.length || 0,
      id: file._id
    }));
}

function getTotalImports(files) {
  return files.reduce((sum, file) => 
    sum + (file.analysis?.dependencies?.imports?.length || 0), 0);
}

function getExternalDependencies(files) {
  const external = new Set();
  
  files.forEach(file => {
    if (file.analysis?.dependencies?.imports) {
      file.analysis.dependencies.imports.forEach(imp => {
        if (!imp.startsWith('./') && !imp.startsWith('../')) {
          external.add(imp);
        }
      });
    }
  });
  
  return Array.from(external);
}

function getInternalDependencies(files) {
  const internal = new Set();
  
  files.forEach(file => {
    if (file.analysis?.dependencies?.imports) {
      file.analysis.dependencies.imports.forEach(imp => {
        if (imp.startsWith('./') || imp.startsWith('../')) {
          internal.add(imp);
        }
      });
    }
  });
  
  return Array.from(internal);
}

function getCloudServiceUsage(files) {
  const services = new Set();
  
  files.forEach(file => {
    if (file.analysis?.cloudServices) {
      file.analysis.cloudServices.forEach(service => {
        services.add(`${service.provider}:${service.service}`);
      });
    }
  });
  
  return Array.from(services);
}

function getRecentActivity(files, limit = 10) {
  return files
    .sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt))
    .slice(0, limit)
    .map(file => ({
      type: 'file_added',
      filename: file.originalName,
      timestamp: file.createdAt,
      details: {
        language: file.language,
        size: file.size,
        complexity: file.analysis?.complexity?.cyclomatic
      }
    }));
}

function getComplexityTrend(files) {
  // Simplified trend calculation based on creation date
  const sortedFiles = files
    .filter(f => f.analysis?.complexity?.cyclomatic)
    .sort((a, b) => new Date(a.createdAt) - new Date(b.createdAt));
  
  return sortedFiles.map(file => ({
    date: file.createdAt,
    complexity: file.analysis.complexity.cyclomatic,
    filename: file.originalName
  }));
}

module.exports = router;