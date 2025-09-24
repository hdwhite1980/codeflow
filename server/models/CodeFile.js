const mongoose = require('mongoose');

const codeFileSchema = new mongoose.Schema({
  filename: {
    type: String,
    required: true
  },
  originalName: {
    type: String,
    required: true
  },
  content: {
    type: String,
    required: true
  },
  projectId: {
    type: mongoose.Schema.Types.ObjectId,
    ref: 'Project',
    required: true
  },
  userId: {
    type: mongoose.Schema.Types.ObjectId,
    ref: 'User',
    required: true
  },
  language: {
    type: String,
    required: true
  },
  size: {
    type: Number,
    required: true
  },
  version: {
    type: Number,
    default: 1
  },
  parentVersion: {
    type: mongoose.Schema.Types.ObjectId,
    ref: 'CodeFile',
    default: null
  },
  analysis: {
    ast: mongoose.Schema.Types.Mixed,
    complexity: {
      cyclomatic: Number,
      maintainabilityIndex: Number,
      halsteadVolume: Number
    },
    security: {
      score: Number,
      issues: [{
        type: String,
        severity: String,
        line: Number,
        description: String
      }]
    },
    dependencies: {
      imports: [String],
      exports: [String],
      functions: [{
        name: String,
        line: Number,
        complexity: Number,
        parameters: [String],
        returnType: String
      }],
      variables: [{
        name: String,
        type: String,
        scope: String,
        line: Number
      }],
      apiCalls: [{
        type: String,
        endpoint: String,
        method: String,
        line: Number
      }],
      databaseOps: [{
        type: String,
        operation: String,
        table: String,
        line: Number
      }]
    },
    cloudServices: [{
      provider: String,
      service: String,
      usage: String,
      line: Number
    }]
  },
  tags: [String],
  createdAt: {
    type: Date,
    default: Date.now
  }
});

module.exports = mongoose.model('CodeFile', codeFileSchema);