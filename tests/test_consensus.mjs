import test from 'node:test';
import assert from 'node:assert/strict';
import {resultAverage, invalidateGradeReview, confirmReview, buildPacket} from '../static/domain.js';

const result = () => ({type:'certificate_9', reviewed:false, auto_grades_verified:true,
  document_series:'АБ', document_number:'0012345', series_not_present:false, identifier_candidates:[],
  grades:[{subject:'Алгебра',grade:5,conflict:false,auto_verified:true,verification:'ocr_consensus'},
          {subject:'Физика',grade:4,conflict:false,auto_verified:true,verification:'ocr_consensus'}],
  warnings:[], text:'', ocr:{third_used:false}});

test('automatic agreement is exported immediately with its OCR evidence', () => {
  const r=result();
  const packet=buildPacket([{name:'scan.png',status:'done',result:r}], '');
  assert.equal(packet.average_grade,4.5);
  assert.equal(packet.documents[0].grades_verification,'automatic');
  assert.equal(packet.review_status,'needs_review');
});

test('editing a grade revokes automatic agreement until the existing confirmation action', () => {
  const r=result(); r.grades[0].grade=3; invalidateGradeReview(r,0);
  assert.equal(r.grades[0].auto_verified,false);
  assert.equal(resultAverage(r),null);
  assert.equal(confirmReview(r),null);
  assert.equal(resultAverage(r),3.5);
  assert.equal(r.grades[0].verification,'manual');
});

test('changing the grade list also requires confirmation', () => {
  const r=result(); r.grades.pop(); invalidateGradeReview(r);
  assert.equal(resultAverage(r),null);
  assert.equal(confirmReview(r),null);
  assert.equal(resultAverage(r),5);
});

test('confirmation cannot turn an unresolved or invalid grade into a result', () => {
  const r=result(); r.grades[0].grade=null; invalidateGradeReview(r,0);
  assert.ok(confirmReview(r));
  assert.equal(r.reviewed,false);
  assert.equal(resultAverage(r),null);
});
