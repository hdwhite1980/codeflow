const express = require('express');
const multer = require('multer');
const path = require('path');
const fs = require('fs').promises;
const CodeFile = require('../models/CodeFile');
const Project = require('../models/Project');
const codeAnalysisService = require('../services/codeAnalysis');

const router = express.Router();

// File upload middleware
const upload = multer({
  dest: path.join(__dirname, '../uploads'),
  limits: { fileSize: 10 * 1024 * 1024 }, // 10MB
  fileFilter: (req, file, cb) => {
    const allowedExtensions = ['.js', '.jsx', '.ts', '.tsx', '.py', '.java', '.cpp', '.c', '.php', '.rb', '.go', '.rs', '.mjs'];
    const fileExtension = path.extname(file.originalname).toLowerCase();
    cb(null, allowedExtensions.includes(fileExtension));
  }
});

/**
 * POST /api/analysis/upload
 * Upload and analyze a code file
 */
router.post('/upload', upload.single('codeFile'), async (req, res) => {
  try {
    if (!req.file) {
      return res.status(400).json({ error: 'No file uploaded' });
    }

    const { projectId } = req.body;
    
    // Read file content
    const content = await fs.readFile(req.file.path, 'utf8');
    const fileExtension = path.extname(req.file.originalname).toLowerCase();
    const language = getLanguageFromExtension(fileExtension);

    // Analyze the code
    const analysis = await codeAnalysisService.analyzeCode(content, req.file.originalname, language);

    // Create CodeFile record
    const codeFile = new CodeFile({
      filename: req.file.filename,
      originalName: req.file.originalname,
      content: content,
      projectId: projectId,
      userId: req.userId || 'anonymous', // Will be set by auth middleware
      language: language,
      size: req.file.size,
      analysis: analysis
    });

    await codeFile.save();

    // Add file to project if projectId provided
    if (projectId) {
      await Project.findByIdAndUpdate(projectId, {
        $push: { files: codeFile._id }
      });
    }

    // Clean up uploaded file
    await fs.unlink(req.file.path);

    res.json({
      success: true,
      file: {
        id: codeFile._id,
        filename: codeFile.originalName,
        language: codeFile.language,
        size: codeFile.size,
        analysis: codeFile.analysis
      }
    });

  } catch (error) {
    console.error('File upload error:', error);
    res.status(500).json({ error: 'Failed to analyze file', details: error.message });
  }
});

/**
 * POST /api/analysis/code
 * Analyze code from text input
 */
router.post('/code', async (req, res) => {
  try {
    const { code, filename, language, projectId } = req.body;

    if (!code || !code.trim()) {
      return res.status(400).json({ error: 'Code content is required' });
    }

    const detectedLanguage = language || detectLanguageFromCode(code) || 'javascript';
    
    // Analyze the code
    const analysis = await codeAnalysisService.analyzeCode(code, filename || 'untitled', detectedLanguage);

    // Create CodeFile record if projectId provided
    let codeFile = null;
    if (projectId) {
      codeFile = new CodeFile({
        filename: filename || `untitled-${Date.now()}`,
        originalName: filename || 'untitled.js',
        content: code,
        projectId: projectId,
        userId: req.userId || 'anonymous',
        language: detectedLanguage,
        size: Buffer.byteLength(code, 'utf8'),
        analysis: analysis
      });

      await codeFile.save();

      // Add file to project
      await Project.findByIdAndUpdate(projectId, {
        $push: { files: codeFile._id }
      });
    }

    res.json({
      success: true,
      analysis: analysis,
      fileId: codeFile ? codeFile._id : null,
      metrics: {
        complexity: analysis.complexity,
        security: analysis.security,
        dependencies: analysis.dependencies,
        cloudServices: analysis.cloudServices,
        codeMetrics: analysis.metrics
      }
    });

  } catch (error) {
    console.error('Code analysis error:', error);
    res.status(500).json({ error: 'Failed to analyze code', details: error.message });
  }
});

/**
 * GET /api/analysis/file/:fileId
 * Get analysis results for a specific file
 */
router.get('/file/:fileId', async (req, res) => {
  try {
    const { fileId } = req.params;
    
    const codeFile = await CodeFile.findById(fileId)
      .populate('projectId', 'name')
      .select('-content'); // Don't send full content unless requested

    if (!codeFile) {
      return res.status(404).json({ error: 'File not found' });
    }

    res.json({
      success: true,
      file: codeFile
    });

  } catch (error) {
    console.error('Get file analysis error:', error);
    res.status(500).json({ error: 'Failed to retrieve file analysis' });
  }
});

/**
 * POST /api/analysis/compare
 * Compare two versions of code files
 */
router.post('/compare', async (req, res) => {
  try {
    const { fileId1, fileId2, code1, code2 } = req.body;

    let analysis1, analysis2;

    if (fileId1 && fileId2) {
      // Compare existing files
      const file1 = await CodeFile.findById(fileId1);
      const file2 = await CodeFile.findById(fileId2);
      
      if (!file1 || !file2) {
        return res.status(404).json({ error: 'One or both files not found' });
      }

      analysis1 = file1.analysis;
      analysis2 = file2.analysis;
    } else if (code1 && code2) {
      // Compare code snippets
      analysis1 = await codeAnalysisService.analyzeCode(code1, 'code1.js');
      analysis2 = await codeAnalysisService.analyzeCode(code2, 'code2.js');
    } else {
      return res.status(400).json({ error: 'Invalid comparison parameters' });
    }

    // Generate comparison report
    const comparison = generateComparisonReport(analysis1, analysis2);

    res.json({
      success: true,
      comparison: comparison
    });

  } catch (error) {
    console.error('Code comparison error:', error);
    res.status(500).json({ error: 'Failed to compare code' });
  }
});

/**
 * GET /api/analysis/project/:projectId/dependencies
 * Get dependency map for entire project
 */
router.get('/project/:projectId/dependencies', async (req, res) => {
  try {
    const { projectId } = req.params;
    
    const project = await Project.findById(projectId).populate('files');
    
    if (!project) {
      return res.status(404).json({ error: 'Project not found' });
    }

    // Build dependency map
    const dependencyMap = buildProjectDependencyMap(project.files);

    res.json({
      success: true,
      projectName: project.name,
      dependencyMap: dependencyMap
    });

  } catch (error) {
    console.error('Project dependency analysis error:', error);
    res.status(500).json({ error: 'Failed to analyze project dependencies' });
  }
});

// Helper functions
function getLanguageFromExtension(ext) {
  const languageMap = {
    '.js': 'javascript',
    '.jsx': 'javascript',
    '.ts': 'typescript',
    '.tsx': 'typescript',
    '.mjs': 'javascript',
    '.py': 'python',
    '.java': 'java',
    '.cpp': 'cpp',
    '.c': 'c',
    '.php': 'php',
    '.rb': 'ruby',
    '.go': 'go',
    '.rs': 'rust'
  };
  
  return languageMap[ext] || 'unknown';
}

function detectLanguageFromCode(code) {
  // Simple language detection heuristics
  if (code.includes('import ') || code.includes('export ') || code.includes('const ') || code.includes('let ')) {
    return 'javascript';
  }
  if (code.includes('def ') || code.includes('import ')) {
    return 'python';
  }
  if (code.includes('public class') || code.includes('private ')) {
    return 'java';
  }
  return 'javascript'; // Default fallback
}

function generateComparisonReport(analysis1, analysis2) {
  return {
    complexity: {
      before: analysis1.complexity,
      after: analysis2.complexity,
      change: analysis2.complexity.cyclomatic - analysis1.complexity.cyclomatic
    },
    security: {
      before: analysis1.security.score,
      after: analysis2.security.score,
      change: analysis2.security.score - analysis1.security.score,
      newIssues: analysis2.security.issues.length - analysis1.security.issues.length
    },
    dependencies: {
      addedImports: analysis2.dependencies.imports.filter(imp => 
        !analysis1.dependencies.imports.includes(imp)
      ),
      removedImports: analysis1.dependencies.imports.filter(imp => 
        !analysis2.dependencies.imports.includes(imp)
      ),
      addedFunctions: analysis2.dependencies.functions.filter(func => 
        !analysis1.dependencies.functions.some(f => f.name === func.name)
      ),
      removedFunctions: analysis1.dependencies.functions.filter(func => 
        !analysis2.dependencies.functions.some(f => f.name === func.name)
      )
    },
    riskLevel: calculateRiskLevel(analysis1, analysis2)
  };
}

function calculateRiskLevel(before, after) {
  let riskScore = 0;
  
  // Complexity increase adds risk
  const complexityIncrease = after.complexity.cyclomatic - before.complexity.cyclomatic;
  riskScore += Math.max(0, complexityIncrease) * 0.5;
  
  // Security score decrease adds risk
  const securityDecrease = before.security.score - after.security.score;
  riskScore += Math.max(0, securityDecrease) * 2;
  
  // New security issues add significant risk
  const newIssues = after.security.issues.length - before.security.issues.length;
  riskScore += Math.max(0, newIssues) * 1;
  
  if (riskScore >= 5) return 'critical';
  if (riskScore >= 3) return 'high';
  if (riskScore >= 1) return 'medium';
  return 'low';
}

function buildProjectDependencyMap(files) {
  const dependencyMap = {
    nodes: [],
    edges: [],
    clusters: []
  };

  files.forEach(file => {
    // Add file as node
    dependencyMap.nodes.push({
      id: file._id.toString(),
      label: file.originalName,
      type: 'file',
      language: file.language,
      complexity: file.analysis.complexity.cyclomatic,
      security: file.analysis.security.score
    });

    // Add functions as nodes
    file.analysis.dependencies.functions.forEach(func => {
      dependencyMap.nodes.push({
        id: `${file._id}_${func.name}`,
        label: func.name,
        type: 'function',
        parent: file._id.toString(),
        complexity: func.complexity,
        line: func.line
      });
    });

    // Add import dependencies as edges
    file.analysis.dependencies.imports.forEach(importPath => {
      const targetFile = files.find(f => 
        f.originalName.includes(importPath.replace(/^\.\//, ''))
      );
      
      if (targetFile) {
        dependencyMap.edges.push({
          from: file._id.toString(),
          to: targetFile._id.toString(),
          type: 'import',
          label: importPath
        });
      }
    });
  });

  return dependencyMap;
}

module.exports = router;